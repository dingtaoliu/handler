import asyncio
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from handler.runtime.types import Lane, Message, Role, Run, RunStatus


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


async def test_ignored_events_do_not_create_or_mark_threads_pending(store, tmp_path, spec, event):
    ignored = event("ignore")
    assert await store.put(ignored, None)
    assert await store.list_threads() == []
    assert await store.pending_events(spec.key) == []
    assert await store.claim("worker", Lane.INTERACTIVE, 10, 60) == []
    assert not await store.put(event("ignore"), spec)
    assert await store.get_thread(spec.key) is None
    with sqlite3.connect(tmp_path / "store.sqlite") as db:
        row = db.execute("SELECT status, consumed_by_run FROM events").fetchone()
    assert row == ("ignored", None)
    assert await store.put(event("routed"), spec)
    assert [e.idempotency_key for e in await store.pending_events(spec.key)] == ["routed"]


async def test_thread_spec_applies_only_once(store, clock, spec, event):
    await store.put(event("first"), spec)
    original = await store.get_thread(spec.key)
    assert original.key == spec.key
    assert original.type is spec.type and original.lane is spec.lane
    assert original.owner_user_id == spec.owner_user_id
    assert original.harness == spec.harness and original.model == spec.model
    assert original.priority == spec.priority
    assert original.created_at == original.pending_since == clock()
    assert original.lease_version == original.attempts == 0
    assert original.dead is False and original.summary is None
    assert original.lease_owner is original.lease_until is original.session_id is None
    assert original.last_error is original.available_at is None
    clock.advance(5)
    await store.put(event("second"), replace(
        spec, owner_user_id="other", lane=Lane.BACKGROUND, harness="other",
        model="other", priority=-1,
    ))
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
    assert await store.claim("w", Lane.INTERACTIVE, -1, 60) == []
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
    assert reclaimed.version == lease.version + 1
    assert reclaimed.owner == "two"
    assert await store.heartbeat(lease, 100) is None
    assert (await store.get_thread(spec.key)).lease_until == reclaimed.until


@pytest.mark.parametrize("wrong", ["owner", "version"])
@pytest.mark.parametrize("method", [
    "heartbeat", "complete", "fail", "set_session", "create_run", "finish_run", "add_messages",
])
async def test_every_fenced_write_rejects_stale_lease(
    store, clock, spec, event, wrong, method,
):
    queued = event("first")
    await store.put(queued, spec)
    old, = await store.claim("worker", Lane.INTERACTIVE, 1, 10)
    run = Run("run", spec.key, old.version, [queued.id], RunStatus.RUNNING, "codex", None)
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
            run, id="stale", lease_version=stale.version,
        )),
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


async def test_complete_consumes_only_snapshot_and_releases_lease(store, clock, spec, event):
    first = event("first")
    await store.put(first, spec)
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
    assert thread.lease_owner is thread.lease_until is None
    assert await store.pending_events(spec.key) == [queued]
    assert await store.claim("worker", Lane.INTERACTIVE, 1, 60) == []
    clock.advance(10)
    lease, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    assert await store.complete(lease, "run", [queued.id])
    thread = await store.get_thread(spec.key)
    assert thread.attempts == 0
    assert thread.last_error is thread.available_at is thread.pending_since is None
    new = event("new")
    await store.put(new, spec)
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


async def test_repeated_failures_increment_attempts_and_preserve_events(store, spec, event):
    queued = event("retry")
    await store.put(queued, spec)
    for attempt in range(1, 4):
        lease, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
        assert await store.fail(lease, f"failure {attempt}", 0)
        thread = await store.get_thread(spec.key)
        assert thread.attempts == attempt
        assert thread.last_error == f"failure {attempt}"
        assert await store.pending_events(spec.key) == [queued]


@pytest.mark.parametrize("signal", ["put", "complete", "fail"])
async def test_wait_for_work_wakes_all_waiters(store, spec, event, signal):
    queued = event("first")
    await store.put(queued, spec)
    lease, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    await store.wait_for_work(0.1)  # Drain the initial put's notification.
    waiters = [asyncio.create_task(store.wait_for_work(60)) for _ in range(3)]
    await asyncio.sleep(0)
    if signal == "put":
        await store.put(event("next"), spec)
    elif signal == "complete":
        await store.complete(lease, "run", [queued.id])
    else:
        await store.fail(lease, "retry", 10)
    await asyncio.wait_for(asyncio.gather(*waiters), 1)


async def test_wait_for_work_remembers_signal_and_times_out(store, spec, event):
    await store.put(event("first"), spec)
    await asyncio.wait_for(store.wait_for_work(60), 1)
    await asyncio.wait_for(store.wait_for_work(0.01), 1)


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
    await asyncio.wait_for(asyncio.gather(*workers), 5)
    assert len(claimed) == len({lease.thread_key for lease in claimed}) == 10
    assert all(lease.version == 1 for lease in claimed)
    events = [await store.pending_events(f"thread-{i}") for i in range(10)]
    assert all(len(group) == 5 for group in events)
    assert len({e.id for group in events for e in group}) == 50
