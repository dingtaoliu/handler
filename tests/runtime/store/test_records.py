import sqlite3
from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from handler.runtime.types import Message, Role, RunStatus, RunStep, StepKind, TokenUsage


async def test_run_create_finish_round_trip(store, clock, running):
    lease, run = running
    assert await store.get_run(run.id) == run
    assert (await store.get_run(run.id)).status is RunStatus.RUNNING
    assert await store.list_runs(run.thread_key) == [run]
    clock.advance(2)
    finished = replace(
        run, status=RunStatus.COMPLETED, finished_at=clock(),
        usage=TokenUsage(200, 120, 30, 50, 10), cost_usd=0.015,
    )
    assert await store.finish_run(lease, finished)
    assert await store.get_run(run.id) == finished
    assert isinstance((await store.get_run(run.id)).usage, TokenUsage)
    # Finishing changes only the fields enumerated in the protocol.
    assert await store.finish_run(lease, replace(
        finished, harness="other", model="other", trigger_event_ids=["other"],
        started_at=clock(), parent_run_id="other",
    ))
    assert await store.get_run(run.id) == finished
    failed = replace(finished, status=RunStatus.FAILED, error="failure", cost_usd=None)
    assert await store.finish_run(lease, failed)
    assert await store.get_run(run.id) == failed
    cancelled = replace(failed, status=RunStatus.CANCELLED)
    assert await store.finish_run(lease, cancelled)
    assert (await store.get_run(run.id)).status is RunStatus.CANCELLED
    assert not await store.finish_run(lease, replace(finished, id="missing"))


async def test_runs_filter_limit_order_parent_and_running_status(store, clock, running):
    lease, run = running
    clock.advance(1)
    child = replace(
        run, id="child", parent_run_id=run.id, started_at=clock(), status=RunStatus.FAILED,
    )
    assert await store.create_run(lease, child)
    child.status = RunStatus.RUNNING
    assert await store.get_run(child.id) == child
    assert await store.list_runs(run.thread_key) == [child, run]
    assert await store.list_runs(run.thread_key, limit=1) == [child]
    assert await store.list_runs("missing") == []
    assert await store.get_run("missing") is None


async def test_lease_cannot_write_records_for_other_threads_or_versions(store, running):
    lease, run = running
    assert not await store.create_run(lease, replace(run, id="other", thread_key="other"))
    assert not await store.create_run(lease, replace(run, id="other", lease_version=0))
    assert not await store.finish_run(lease, replace(run, thread_key="other"))
    assert not await store.finish_run(lease, replace(run, lease_version=0))
    assert not await store.add_messages(lease, [Message("other", Role.USER, [])])
    assert await store.list_messages(run.thread_key) == []
    assert await store.get_run("other") is None


async def test_session_and_thread_listing(store, clock, spec, event, running):
    lease, run = running
    assert await store.set_session(lease, "session-1")
    assert (await store.get_thread(spec.key)).session_id == "session-1"
    clock.advance(1)
    other = replace(spec, key="other", owner_user_id="someone-else")
    await store.put(event("other"), other)
    threads = await store.list_threads()
    assert [t.key for t in threads] == [other.key, spec.key]
    assert [t.key for t in await store.list_threads(owner_user_id="u")] == [spec.key]
    assert await store.list_threads(owner_user_id="nobody") == []
    assert [t.key for t in await store.list_threads(limit=1)] == [other.key]
    assert await store.get_thread("missing") is None


async def test_message_round_trip_ordering_limit_and_before(store, clock, running):
    lease, run = running
    first = Message(
        run.thread_key, Role.USER, [{"type": "text", "text": "你好"}],
        run_id=run.id, raw={"nested": {"value": [1, True, None]}}, created_at=clock(),
    )
    second = replace(
        first, id="second", role=Role.ASSISTANT, raw=None,
        created_at=clock() + timedelta(seconds=1),
    )
    tied = replace(second, id="tied", role=Role.TOOL, raw={})
    last = replace(first, id="last", role=Role.EVENT, created_at=clock() + timedelta(seconds=2))
    assert await store.add_messages(lease, [last, second, tied, first])
    assert await store.list_messages(run.thread_key) == [first, second, tied, last]
    assert await store.list_messages(run.thread_key, limit=2) == [first, second]
    assert await store.list_messages(run.thread_key, before=second.created_at) == [first]
    assert await store.list_messages(run.thread_key, before=last.created_at) == [first, second, tied]
    assert await store.list_messages("missing") == []
    assert await store.add_messages(lease, [])


async def test_message_batch_is_atomic_on_constraint_error(store, clock, running):
    lease, run = running
    original = Message(run.thread_key, Role.USER, [], id="existing", created_at=clock())
    assert await store.add_messages(lease, [original])
    with pytest.raises(sqlite3.IntegrityError):
        await store.add_messages(lease, [replace(original, id="new"), original])
    assert await store.list_messages(run.thread_key) == [original]
    assert await store.add_messages(lease, [replace(original, id="after-error")])
    assert len(await store.list_messages(run.thread_key)) == 2


async def test_steps_add_update_list_are_unfenced_and_preserve_identity(store, clock, running):
    lease, run = running
    tool = RunStep(
        run.id, 1, StepKind.TOOL_CALL, "shell", "stream", started_at=clock(),
        status="running", model="test-model", data={"id": "item-1", "input": "pwd"},
        raw_ref="run.jsonl#L1", parent_seq=2,
    )
    llm = RunStep(
        run.id, 2, StepKind.LLM_CALL, "model", "proxy", started_at=clock(),
        usage=TokenUsage(50, 10, 2, 5, 1), cost_usd=0.01,
    )
    await store.add_step(llm)
    await store.add_step(tool)
    assert await store.list_steps(run.id) == [tool, llm]
    assert await store.list_steps("missing") == []
    # Observability can still finish a step after the lease has been released.
    assert await store.fail(lease, "failed", None)
    clock.advance(3)
    finished = replace(
        tool, ended_at=clock(), status="error", usage=TokenUsage(10, 2, 1, 4, 1),
        cost_usd=0.001, data={"output": "failure", "exit_code": 1},
    )
    await store.update_step(replace(
        finished, kind=StepKind.ERROR, name="different", source="hook", model="different",
        started_at=clock(), raw_ref="other.jsonl#L2", parent_seq=None,
    ))
    assert await store.list_steps(run.id) == [finished, llm]
    await store.update_step(replace(finished, seq=99))
    assert await store.list_steps(run.id) == [finished, llm]
    with pytest.raises(sqlite3.IntegrityError):
        await store.add_step(tool)
    assert await store.list_steps(run.id) == [finished, llm]


async def test_timestamps_normalized_to_utc_and_events_sorted(store, clock, spec, event):
    offset = timezone(timedelta(hours=-4))
    local = clock().astimezone(offset)
    first = event("first", received_at=local)
    last = event("last", received_at=local + timedelta(seconds=2))
    middle = event("middle", received_at=local + timedelta(seconds=1))
    await store.put(last, spec)
    await store.put(first, spec)
    await store.put(middle, spec)
    events = await store.pending_events(spec.key)
    assert events == [first, middle, last]
    assert all(e.received_at.utcoffset() == timedelta(0) for e in events)
