"""Queue/fencing/record scenarios run against both SQLiteCore and PostgresCore."""

import asyncio
from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from handler.runtime.types import (
    Lane, Message, Role, Run, RunStatus, RunStep, StepKind, TokenUsage,
)


async def test_put_is_idempotent_without_rerouting(store, clock, spec, event):
    original = event("same", payload={"nested": [1, True, None, "你好"]})
    assert await store.put(original, spec)
    first_thread = await store.get_thread(spec.key)
    clock.advance(10)
    assert not await store.put(
        event("same", payload={"changed": True}), replace(spec, key="other"),
    )
    assert await store.pending_events(spec.key) == [original]
    assert await store.get_thread(spec.key) == first_thread
    assert await store.get_thread("other") is None


async def test_ignored_events_never_create_threads(store, spec, event):
    assert await store.put(event("ignore"), None)
    assert await store.list_threads() == []
    assert await store.pending_events(spec.key) == []
    assert await store.claim("worker", Lane.INTERACTIVE, 10, 60) == []
    assert not await store.put(event("ignore"), spec)
    assert await store.get_thread(spec.key) is None


async def test_thread_spec_applies_only_once(store, clock, spec, event):
    await store.put(event("first"), spec)
    original = await store.get_thread(spec.key)
    assert original.type is spec.type and original.lane is spec.lane
    assert original.harness == spec.harness and original.priority == spec.priority
    assert original.created_at == original.pending_since == clock()
    assert original.dead is False and original.lease_version == 0
    clock.advance(5)
    await store.put(event("second"), replace(spec, owner_user_id="x", priority=-1))
    assert await store.get_thread(spec.key) == original


async def test_claim_orders_priority_then_pending_since_and_filters_lane(
    store, clock, spec, event,
):
    for key, priority, lane in [
        ("low", 5, Lane.INTERACTIVE), ("first", 0, Lane.INTERACTIVE),
        ("second", 0, Lane.INTERACTIVE), ("background", -5, Lane.BACKGROUND),
    ]:
        await store.put(event(key), replace(spec, key=key, priority=priority, lane=lane))
        clock.advance(1)
    assert await store.claim("w", Lane.INTERACTIVE, 0, 60) == []
    leases = await store.claim("w", Lane.INTERACTIVE, 2, 60)
    assert [lease.thread_key for lease in leases] == ["first", "second"]
    assert all(lease.version == 1 and lease.until == clock() + timedelta(seconds=60)
               for lease in leases)
    assert [l.thread_key for l in await store.claim("w2", Lane.INTERACTIVE, 10, 60)] == ["low"]
    assert [l.thread_key for l in await store.claim("w3", Lane.BACKGROUND, 10, 60)] == ["background"]


async def test_two_owners_cannot_claim_same_thread(store, spec, event):
    await store.put(event("first"), spec)
    results = await asyncio.gather(*[
        store.claim(owner, Lane.INTERACTIVE, 1, 60) for owner in ["one", "two"]
    ])
    assert sorted(map(len, results)) == [0, 1]
    lease = next(leases[0] for leases in results if leases)
    thread = await store.get_thread(spec.key)
    assert (thread.lease_owner, thread.lease_version, thread.lease_until) == (
        lease.owner, lease.version, lease.until,
    )


async def test_lease_expiry_and_heartbeat(store, clock, spec, event):
    await store.put(event("first"), spec)
    lease, = await store.claim("one", Lane.INTERACTIVE, 1, 10)
    clock.advance(5)
    extended = await store.heartbeat(lease, 10)
    assert extended == replace(lease, until=clock() + timedelta(seconds=10))
    clock.advance(10)
    assert await store.claim("two", Lane.INTERACTIVE, 1, 10) == []
    clock.advance(0.000001)
    reclaimed, = await store.claim("two", Lane.INTERACTIVE, 1, 10)
    assert reclaimed.version == lease.version + 1 and reclaimed.owner == "two"
    assert await store.heartbeat(lease, 100) is None
    assert (await store.get_thread(spec.key)).lease_until == reclaimed.until


@pytest.mark.parametrize("wrong", ["owner", "version"])
@pytest.mark.parametrize("method", [
    "heartbeat", "complete", "fail", "set_session", "create_run", "finish_run",
    "add_messages",
])
async def test_every_fenced_write_rejects_stale_lease(
    store, clock, spec, event, wrong, method,
):
    queued = event("first")
    await store.put(queued, spec)
    old, = await store.claim("worker", Lane.INTERACTIVE, 1, 10)
    run = Run("run", spec.key, old.version, [queued.id], RunStatus.RUNNING, "codex", None,
              started_at=clock())
    assert await store.create_run(old, run)
    clock.advance(11)
    current, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    stale = old if wrong == "version" else replace(current, owner="other")
    before = await store.get_thread(spec.key)
    calls = {
        "heartbeat": lambda: store.heartbeat(stale, 100),
        "complete": lambda: store.complete(stale, "run", [queued.id]),
        "fail": lambda: store.fail(stale, "stale", None),
        "set_session": lambda: store.set_session(stale, "stale"),
        "create_run": lambda: store.create_run(stale, replace(
            run, id="stale", lease_version=stale.version)),
        "finish_run": lambda: store.finish_run(stale, replace(run, status=RunStatus.FAILED)),
        "add_messages": lambda: store.add_messages(stale, [Message(spec.key, Role.USER, [])]),
    }
    result = await calls[method]()
    assert result is None if method == "heartbeat" else result is False
    assert await store.get_thread(spec.key) == before
    assert await store.pending_events(spec.key) == [queued]
    assert await store.get_run("run") == run
    assert await store.get_run("stale") is None
    assert await store.list_messages(spec.key) == []


async def test_complete_consumes_only_snapshot_and_keeps_pending(store, clock, spec, event):
    await store.put(event("first"), spec)
    lease, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    snapshot = await store.pending_events(spec.key)
    clock.advance(2)
    arrival = event("new")
    await store.put(arrival, spec)
    other = event("other", thread_key="other")
    await store.put(other, replace(spec, key="other"))
    assert await store.complete(lease, "run", [e.id for e in snapshot] + [other.id])
    assert await store.pending_events(spec.key) == [arrival]
    assert await store.pending_events("other") == [other]
    thread = await store.get_thread(spec.key)
    assert thread.pending_since is not None
    assert thread.lease_owner is thread.lease_until is None
    assert not await store.set_session(lease, "released")
    new_lease = next(l for l in await store.claim("worker", Lane.INTERACTIVE, 10, 60)
                     if l.thread_key == spec.key)
    assert await store.complete(new_lease, "run2", [arrival.id])
    assert await store.pending_events(spec.key) == []
    assert (await store.get_thread(spec.key)).pending_since is None


async def test_fail_backoff_reset_and_dead_letter(store, clock, spec, event):
    queued = event("first")
    await store.put(queued, spec)
    lease, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    assert await store.fail(lease, "try again", 10)
    thread = await store.get_thread(spec.key)
    assert thread.attempts == 1 and thread.last_error == "try again"
    assert thread.available_at == clock() + timedelta(seconds=10)
    assert thread.dead is False and thread.pending_since is not None
    assert await store.claim("worker", Lane.INTERACTIVE, 1, 60) == []
    clock.advance(10)
    lease, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    assert await store.complete(lease, "run", [queued.id])
    thread = await store.get_thread(spec.key)
    assert thread.attempts == 0
    assert thread.last_error is thread.available_at is thread.pending_since is None
    await store.put(event("new"), spec)
    lease, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    assert await store.fail(lease, "fatal", None)
    thread = await store.get_thread(spec.key)
    assert thread.dead is True and thread.attempts == 1 and thread.last_error == "fatal"
    await store.put(event("after-dead", type="crontal.rfq.updated"), spec)
    clock.advance(1000)
    assert await store.claim("worker", Lane.INTERACTIVE, 1, 60) == []
    assert len(await store.pending_events(spec.key)) == 2
    # A new chat message revives the dead thread with its pending events.
    await store.put(event("user-again"), spec)
    thread = await store.get_thread(spec.key)
    assert thread.dead is False and thread.attempts == 0
    lease, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    assert len(await store.pending_events(spec.key)) == 3


@pytest.mark.parametrize("signal", ["put", "complete", "fail"])
async def test_wait_for_work_wakes(store, spec, event, signal):
    queued = event("first")
    await store.put(queued, spec)
    lease, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    await store.wait_for_work(0.1)
    waiter = asyncio.create_task(store.wait_for_work(60))
    await asyncio.sleep(0.05)
    if signal == "put":
        await store.put(event("next"), spec)
    elif signal == "complete":
        await store.complete(lease, "run", [queued.id])
    else:
        await store.fail(lease, "retry", 10)
    await asyncio.wait_for(waiter, 2)


async def test_wait_for_work_times_out(store):
    await store.wait_for_work(0.05)
    await asyncio.wait_for(store.wait_for_work(0.05), 2)


async def test_fifty_puts_and_three_concurrent_claimers(store, spec, event):
    claimed = []
    producers_done = asyncio.Event()

    async def producer(i):
        await store.put(event(str(i)), replace(spec, key=f"thread-{i % 10}"))

    async def claimer(owner):
        while not producers_done.is_set():
            claimed.extend(await store.claim(owner, Lane.INTERACTIVE, 2, 60))
            await asyncio.sleep(0)
        claimed.extend(await store.claim(owner, Lane.INTERACTIVE, 10, 60))

    workers = [asyncio.create_task(claimer(str(i))) for i in range(3)]
    await asyncio.gather(*(producer(i) for i in range(50)))
    producers_done.set()
    await asyncio.wait_for(asyncio.gather(*workers), 10)
    assert len(claimed) == len({lease.thread_key for lease in claimed}) == 10
    events = [await store.pending_events(f"thread-{i}") for i in range(10)]
    assert all(len(group) == 5 for group in events)


async def test_run_round_trip_and_listing(store, clock, running):
    lease, run = running
    assert await store.get_run(run.id) == run
    clock.advance(2)
    finished = replace(run, status=RunStatus.COMPLETED, finished_at=clock(),
                       usage=TokenUsage(200, 120, 30, 50, 10), cost_usd=0.015)
    assert await store.finish_run(lease, finished)
    assert await store.get_run(run.id) == finished
    assert isinstance((await store.get_run(run.id)).usage, TokenUsage)
    assert await store.finish_run(lease, replace(finished, harness="other"))
    assert await store.get_run(run.id) == finished
    assert not await store.finish_run(lease, replace(finished, id="missing"))
    clock.advance(1)
    child = replace(run, id="child", parent_run_id=run.id, started_at=clock(),
                    status=RunStatus.FAILED)
    assert await store.create_run(lease, child)
    child.status = RunStatus.RUNNING
    assert await store.list_runs(run.thread_key) == [child, finished]
    assert await store.list_runs(run.thread_key, limit=1) == [child]
    assert not await store.create_run(lease, replace(run, id="o", thread_key="other"))


async def test_session_and_thread_listing(store, clock, spec, event, running):
    lease, _ = running
    assert await store.set_session(lease, "session-1")
    assert (await store.get_thread(spec.key)).session_id == "session-1"
    clock.advance(1)
    other = replace(spec, key="other", owner_user_id="someone-else")
    await store.put(event("other"), other)
    assert [t.key for t in await store.list_threads()] == [other.key, spec.key]
    assert [t.key for t in await store.list_threads(owner_user_id="u")] == [spec.key]
    assert [t.key for t in await store.list_threads(limit=1)] == [other.key]


async def test_message_round_trip_ordering_limit_and_before(store, clock, running):
    lease, run = running
    first = Message(run.thread_key, Role.USER, [{"type": "text", "text": "你好"}],
                    run_id=run.id, raw={"nested": {"value": [1, True, None]}},
                    created_at=clock())
    second = replace(first, id="second", role=Role.ASSISTANT, raw=None,
                     created_at=clock() + timedelta(seconds=1))
    tied = replace(second, id="tied", role=Role.TOOL, raw={})
    last = replace(first, id="last", role=Role.EVENT,
                   created_at=clock() + timedelta(seconds=2))
    assert await store.add_messages(lease, [last, second, tied, first])
    assert await store.list_messages(run.thread_key) == [first, second, tied, last]
    assert await store.list_messages(run.thread_key, limit=2) == [first, second]
    assert await store.list_messages(run.thread_key, before=second.created_at) == [first]
    assert await store.add_messages(lease, [])


async def test_message_batch_is_atomic(store, clock, running):
    lease, run = running
    original = Message(run.thread_key, Role.USER, [], id="existing", created_at=clock())
    assert await store.add_messages(lease, [original])
    with pytest.raises(Exception):
        await store.add_messages(lease, [replace(original, id="new"), original])
    assert await store.list_messages(run.thread_key) == [original]
    assert await store.add_messages(lease, [replace(original, id="after")])


async def test_steps_add_update_list_unfenced(store, clock, running):
    lease, run = running
    tool = RunStep(run.id, 1, StepKind.TOOL_CALL, "shell", "stream", started_at=clock(),
                   status="running", model="m", data={"id": "item-1"},
                   raw_ref="run.jsonl#L1", parent_seq=2)
    llm = RunStep(run.id, 2, StepKind.LLM_CALL, "model", "proxy", started_at=clock(),
                  usage=TokenUsage(50, 10, 2, 5, 1), cost_usd=0.01)
    await store.add_step(llm)
    await store.add_step(tool)
    assert await store.list_steps(run.id) == [tool, llm]
    assert await store.fail(lease, "failed", None)
    clock.advance(3)
    finished = replace(tool, ended_at=clock(), status="error",
                       usage=TokenUsage(10, 2, 1, 4, 1), cost_usd=0.001,
                       data={"output": "failure"})
    await store.update_step(replace(finished, kind=StepKind.ERROR, name="different"))
    assert await store.list_steps(run.id) == [finished, llm]
    await store.update_step(replace(finished, seq=99))
    with pytest.raises(Exception):
        await store.add_step(tool)
    assert await store.list_steps(run.id) == [finished, llm]


async def test_timestamps_normalized_to_utc_and_events_sorted(store, clock, spec, event):
    local = clock().astimezone(timezone(timedelta(hours=-4)))
    first = event("first", received_at=local)
    middle = event("middle", received_at=local + timedelta(seconds=1))
    last = event("last", received_at=local + timedelta(seconds=2))
    for e in (last, first, middle):
        await store.put(e, spec)
    events = await store.pending_events(spec.key)
    assert events == [first, middle, last]
    assert all(e.received_at.utcoffset() == timedelta(0) for e in events)


async def test_list_events_filters_and_orders_newest_first(store, clock, spec, event):
    first = event("a")
    await store.put(first, spec)
    clock.advance(1)
    ignored = event("b")
    await store.put(ignored, None)
    rows = await store.list_events()
    assert [(e.idempotency_key, status) for e, status in rows] == [
        ("b", "ignored"), ("a", "routed")]
    assert [e.id for e, _ in await store.list_events(status="ignored")] == [ignored.id]
    assert await store.list_events(source="nowhere") == []
    assert len(await store.list_events(limit=1)) == 1


async def test_nul_characters_do_not_break_writes(store, clock, running):
    # Postgres rejects "\u0000" in text and jsonb; a binary file read as text has them.
    lease, run = running
    step = RunStep(run.id, 1, StepKind.TOOL_CALL, "read_file", "stream", started_at=clock(),
                   status="running", data={"input": "a\x00b"})
    await store.add_step(step)
    await store.update_step(replace(step, status="ok", data={"output": "PK\x00\x00xls",
                                                             "k\x00": ["\x00"]}))
    message = Message(run.thread_key, Role.TOOL, [{"type": "tool_result", "output": "x\x00y"}],
                      run_id=run.id)
    assert await store.add_messages(lease, [message])
    assert await store.finish_run(lease, replace(run, status=RunStatus.FAILED,
                                                 error="bad \x00 byte", finished_at=clock()))
    (saved,) = await store.list_steps(run.id)
    assert saved.data["output"].replace("\x00", "") == "PKxls"
    (stored,) = await store.list_messages(run.thread_key)
    assert stored.blocks[0]["output"].replace("\x00", "") == "xy"
    assert (await store.get_run(run.id)).error.replace("\x00", "") == "bad  byte"


async def test_sweep_marks_runs_without_a_live_lease_abandoned(store, clock, running):
    lease, run = running
    assert await store.sweep_abandoned_runs() == []  # its lease is live
    clock.advance(61)
    assert await store.sweep_abandoned_runs(run.thread_key) == [run.id]
    swept = await store.get_run(run.id)
    assert swept.status is RunStatus.ABANDONED and swept.finished_at is not None
    assert "abandoned" in swept.error
    assert await store.sweep_abandoned_runs() == []
    assert not await store.abandon_run(run.id, "again")  # only 'running' rows change


async def test_abandon_run_marks_a_running_row(store, running):
    _, run = running
    assert await store.abandon_run(run.id, "abandoned: lease lost mid-run")
    abandoned = await store.get_run(run.id)
    assert abandoned.status is RunStatus.ABANDONED and abandoned.error.startswith("abandoned")


async def test_revive_thread_clears_dead_and_optionally_session(store, spec, running):
    lease, _ = running
    assert await store.set_session(lease, "conv_1")
    assert await store.fail(lease, "fatal", None)
    assert (await store.get_thread(spec.key)).dead
    assert await store.revive_thread(spec.key, reset_session=True)
    thread = await store.get_thread(spec.key)
    assert not thread.dead and thread.attempts == 0 and thread.last_error is None
    assert not thread.session_id
    assert len(await store.claim("worker", spec.lane, 1, 60)) == 1
    assert not await store.revive_thread("missing")


async def test_revive_thread_can_discard_pending_events(store, spec, running):
    lease, _ = running
    assert await store.fail(lease, "fatal", None)
    assert await store.revive_thread(spec.key, discard_pending=True)
    assert await store.pending_events(spec.key) == []
    assert await store.claim("worker", spec.lane, 1, 60) == []
