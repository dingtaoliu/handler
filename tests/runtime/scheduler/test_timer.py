"""The host timer over due work (SQLite; Postgres in store_pg/test_due_work.py)."""
import asyncio
from datetime import timedelta

import pytest

from handler.runtime.integrations.base import TimerKind
from handler.runtime.scheduler import Scheduler
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.timer import HostTimer, TenantDirectory, TimerTarget, backoff_seconds
from handler.runtime.types import Schedule, utcnow


@pytest.fixture
async def full(tmp_path):
    store = SQLiteStore(tmp_path / "handler.db")
    try:
        yield store
    finally:
        await store.close()


def host_for(store, kinds, context=None, **timer):
    directory = TenantDirectory()
    target = TimerTarget("main", store.due_tenant, store.due_queue())
    for kind in kinds:
        target.bind(kind, context)
    directory.register(target)
    timer = HostTimer(directory, **timer)
    store.on_due_changed = timer.wake
    return timer


def schedule_kind(scheduler):
    return TimerKind("schedule.fire", lambda _c, item: scheduler.fire_due(item.ref))


async def rows(store, kind=None):
    return [r for r in await store.due_rows() if kind is None or r["kind"] == kind]


async def test_schedule_rows_follow_their_schedule_in_the_same_write(full):
    schedule = Schedule("s", "p", "every 1h", next_run_at=utcnow() + timedelta(hours=1))
    await full.create_schedule(schedule)
    row, = await rows(full, "schedule.fire")
    assert row["ref"] == schedule.id and row["due_at"] == schedule.next_run_at.isoformat()
    schedule.next_run_at = utcnow() + timedelta(hours=2)
    await full.update_schedule(schedule)
    row, = await rows(full, "schedule.fire")
    assert row["due_at"] == schedule.next_run_at.isoformat() and row["version"] == 2
    schedule.enabled = False
    await full.update_schedule(schedule)
    assert await rows(full, "schedule.fire") == []
    schedule.enabled = True
    await full.update_schedule(schedule)
    assert await full.delete_schedule(schedule.id)
    assert await rows(full, "schedule.fire") == []


async def test_a_missed_schedule_fires_once_and_moves_to_its_next_slot_from_now(full):
    missed = utcnow() - timedelta(days=3)
    schedule = Schedule("Missed", "Prompt", "every 30m", next_run_at=missed)
    await full.create_schedule(schedule)
    timer = host_for(full, [schedule_kind(Scheduler(full, default_harness="fake"))])
    assert await timer.run_due() == 1
    events = await full.pending_events(f"automation:{schedule.id}")
    assert len(events) == 1
    stored = await full.get_schedule(schedule.id)
    assert stored.next_run_at > utcnow() + timedelta(minutes=29)
    row, = await rows(full, "schedule.fire")
    assert row["due_at"] == stored.next_run_at.isoformat() and row["lease_owner"] is None
    assert await timer.run_due() == 0  # not due again until then


async def test_the_timer_sleeps_until_the_earliest_row_and_wakes_on_a_new_one(full):
    scheduler = Scheduler(full, default_harness="fake")
    timer = host_for(full, [schedule_kind(scheduler)], poll_seconds=3600)
    stop = asyncio.Event()
    task = asyncio.create_task(timer.run(stop))
    try:
        await asyncio.sleep(0.05)  # asleep with nothing due
        when = utcnow() + timedelta(seconds=0.3)
        schedule = Schedule("soon", "p", f"at: {when.isoformat()}", one_shot=True,
                            next_run_at=when)
        await full.create_schedule(schedule)  # the store's hint wakes the timer
        for _ in range(100):
            if await full.pending_events(f"automation:{schedule.id}"):
                break
            await asyncio.sleep(0.02)
        assert await full.pending_events(f"automation:{schedule.id}")
        assert utcnow() >= when
        await asyncio.sleep(0.05)
        assert await rows(full, "schedule.fire") == []  # one shot: nothing more due
    finally:
        stop.set()
        await asyncio.wait_for(task, 2)


async def test_a_failure_is_recorded_with_backoff_and_never_stops_the_timer(full):
    calls = []

    async def broken(_context, item):
        calls.append(item.ref)
        raise RuntimeError("boom")

    async def fine(_context, item):
        calls.append(item.ref)
        return None

    await full.set_due("test.broken", "a", utcnow())
    await full.set_due("test.fine", "b", utcnow())
    timer = host_for(full, [TimerKind("test.broken", broken), TimerKind("test.fine", fine)])
    assert await timer.run_due() == 2
    assert sorted(calls) == ["a", "b"]
    row, = await rows(full)
    assert (row["kind"], row["attempts"], row["last_error"]) == ("test.broken", 1, "RuntimeError: boom")
    assert row["lease_owner"] is None
    assert row["due_at"] >= (utcnow() + timedelta(seconds=backoff_seconds(1) - 5)).isoformat()
    assert backoff_seconds(2) == 2 * backoff_seconds(1) and backoff_seconds(99) == 3600


async def test_a_rewrite_during_the_handler_wins_over_its_result(full):
    later = utcnow() + timedelta(hours=5)

    async def edits(_context, item):
        await full.set_due("test.kind", item.ref, later)  # e.g. a person edited the work
        return utcnow() + timedelta(minutes=1)

    await full.set_due("test.kind", "x", utcnow())
    timer = host_for(full, [TimerKind("test.kind", edits)])
    await timer.run_due()
    row, = await rows(full)
    assert row["due_at"] == later.isoformat() and row["lease_owner"] is None


async def test_rows_of_unknown_kinds_are_removed_and_handlers_see_their_context(full):
    seen = []

    async def handler(context, item):
        seen.append(context)
        return None

    await full.set_due("gone.integration", "x", utcnow())
    await full.set_due("test.kind", "y", utcnow())
    timer = host_for(full, [TimerKind("test.kind", handler)], context="ctx")
    await timer.run_due()
    assert seen == ["ctx"] and await rows(full) == []


async def test_a_handler_that_runs_too_long_times_out_and_is_retried(full):
    async def slow(_context, item):
        await asyncio.sleep(10)

    await full.set_due("test.slow", "x", utcnow())
    timer = host_for(full, [TimerKind("test.slow", slow, timeout_seconds=0.05)])
    await timer.run_due()
    row, = await rows(full)
    assert row["attempts"] == 1 and row["last_error"] == "TimeoutError"


async def test_expiry_rows_come_and_go_with_pending_approvals_and_questions(full):
    from handler.runtime.types import Approval, ApprovalStatus, Question, QuestionStatus

    approval = Approval(run_id="r", thread_key="chat:u:1", tool="t", action="a", args={},
                        requested_by="u", reason="", expires_at=utcnow() + timedelta(days=1))
    await full.create_approval(approval)
    question = Question(run_id="r", thread_key="chat:u:1", asked_by="u", question="q?",
                        options=[], expires_at=utcnow() + timedelta(days=1))
    await full.create_question(question)
    assert {(r["kind"], r["ref"]) for r in await rows(full)} == {
        ("approval.expire", approval.id), ("question.expire", question.id)}
    assert await full.transition_approval(approval.id, ApprovalStatus.PENDING,
                                          ApprovalStatus.APPROVED, decided_by="u")
    assert await full.transition_question(question.id, QuestionStatus.PENDING,
                                          QuestionStatus.CANCELED)
    assert await rows(full) == []


async def test_backfill_is_idempotent_and_covers_rows_from_before_due_work(full):
    schedule = Schedule("s", "p", "every 1h", next_run_at=utcnow() - timedelta(hours=2))
    await full.create_schedule(schedule)
    await full._call(lambda db: db.execute("DELETE FROM due_work"))  # as written by v65
    await full.set_due("schedule.fire", "sch_deleted", utcnow())
    await full.backfill_due_work()
    await full.backfill_due_work()
    found = {(r["kind"], r["ref"]) for r in await rows(full)}
    assert found == {("schedule.fire", schedule.id), ("retention.cleanup", "-")}
    await full.backfill_due_work(schedules=False)  # a plan without automations
    assert {r["kind"] for r in await rows(full)} == {"retention.cleanup"}
