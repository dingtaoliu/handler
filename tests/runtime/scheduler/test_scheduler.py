import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from handler.runtime.scheduler import Scheduler
from handler.runtime.types import Lane, Schedule, ThreadType, utcnow


async def test_tick_payload_routing_and_advancement(store, clock):
    schedule = Schedule(
        "Digest", "Summarize the inbox", "every 30m", created_by="u", notify=["u", "v"],
        harness="custom", model="test-model", parent_thread_key="chat:u:c",
        next_run_at=clock(),
    )
    await store.create_schedule(schedule)
    scheduler = Scheduler(store, clock=clock)
    assert await scheduler.tick() == 1
    assert await scheduler.tick() == 0
    events = await store.pending_events(f"automation:{schedule.id}")
    assert len(events) == 1
    event = events[0]
    assert event.type == "automation.fired" and event.source == "scheduler"
    assert event.user_id == "u" and event.received_at == clock()
    assert event.idempotency_key == f"automation:{schedule.id}:{clock().isoformat()}"
    assert event.payload == {
        "schedule_id": schedule.id, "name": "Digest", "prompt": "Summarize the inbox",
        "fired_at": clock().isoformat(), "notify": ["u", "v"], "parent_thread_key": "chat:u:c",
    }
    thread = await store.get_thread(event.thread_key)
    assert thread.type == ThreadType.AUTOMATION and thread.lane == Lane.BACKGROUND
    assert thread.harness == "custom" and thread.model == "test-model"
    assert thread.owner_user_id == "u"
    stored = await store.get_schedule(schedule.id)
    assert stored.next_run_at == clock() + timedelta(minutes=30)
    assert stored.last_run_at == clock()


@pytest.mark.parametrize("spec,expected_delta", [("every 30m", 1800), ("cron: 0 * * * *", 3600)])
async def test_tick_collapses_missed_runs(store, clock, spec, expected_delta):
    missed = clock() - timedelta(days=3)
    schedule = Schedule("Missed", "Prompt", spec, next_run_at=missed)
    await store.create_schedule(schedule)
    scheduler = Scheduler(store, clock=clock, default_harness="fallback")
    assert await scheduler.tick() == 1
    stored = await store.get_schedule(schedule.id)
    assert stored.next_run_at == clock() + timedelta(seconds=expected_delta)
    assert stored.last_run_at == missed
    key = f"automation:{schedule.id}"
    event, = await store.pending_events(key)
    assert event.idempotency_key == f"automation:{schedule.id}:{missed.isoformat()}"
    assert event.payload["fired_at"] == clock().isoformat()
    assert (await store.get_thread(key)).harness == "fallback"
    assert await scheduler.tick() == 0


async def test_tick_preserves_cadence_before_catchup(store, clock):
    expected = clock() - timedelta(minutes=10)
    schedule = Schedule("s", "p", "every 30m", next_run_at=expected)
    await store.create_schedule(schedule)
    assert await Scheduler(store, clock=clock).tick() == 1
    assert (await store.get_schedule(schedule.id)).next_run_at == expected + timedelta(minutes=30)


async def test_tick_one_shot_future_and_disabled(store, clock):
    schedules = [
        Schedule("once", "p", f"at: {(clock() - timedelta(days=1)).isoformat()}",
                 one_shot=True, next_run_at=clock() - timedelta(days=1)),
        Schedule("future", "p", "every 1h", next_run_at=clock() + timedelta(hours=1)),
        Schedule("disabled", "p", "every 1h", enabled=False, next_run_at=clock()),
    ]
    for schedule in schedules:
        await store.create_schedule(schedule)
    scheduler = Scheduler(store, clock=clock)
    assert await scheduler.tick() == 1
    stored = await store.get_schedule(schedules[0].id)
    assert stored.enabled is False and stored.next_run_at is None
    assert await scheduler.tick() == 0


async def test_tick_fires_all_due_across_batches(store, clock):
    for i in range(107):
        await store.create_schedule(Schedule(str(i), "p", "every 1h", next_run_at=clock()))
    assert await Scheduler(store, clock=clock).tick() == 107
    assert await store.due_schedules(clock()) == []


async def test_competing_scheduler_ticks(store, clock):
    for i in range(5):
        await store.create_schedule(Schedule(str(i), "p", "every 1h", next_run_at=clock()))
    counts = await asyncio.gather(*[Scheduler(store, clock=clock).tick() for _ in range(2)])
    assert sum(counts) == 5
    for schedule in await store.list_schedules():
        assert len(await store.pending_events(f"automation:{schedule.id}")) == 1


async def start_sleeping_scheduler(store, clock, monkeypatch):
    sleeping = asyncio.Event()
    original = store.next_due_at

    async def next_due_at():
        next_due = await original()
        sleeping.set()
        return next_due

    monkeypatch.setattr(store, "next_due_at", next_due_at)
    scheduler = Scheduler(store, clock=clock, max_sleep_seconds=3600)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_forever(stop))
    await asyncio.wait_for(sleeping.wait(), timeout=1)
    return scheduler, stop, task, sleeping


async def test_run_forever_wake_and_stop(store, clock, monkeypatch):
    scheduler, stop, task, sleeping = await start_sleeping_scheduler(store, clock, monkeypatch)
    fired = asyncio.Event()
    original = store.fire_schedule

    async def fire(*args):
        result = await original(*args)
        fired.set()
        return result

    monkeypatch.setattr(store, "fire_schedule", fire)
    try:
        schedule = Schedule("new", "p", "every 1h", next_run_at=clock())
        await store.create_schedule(schedule)
        scheduler.wake()
        await asyncio.wait_for(fired.wait(), timeout=1)
        assert (await store.get_schedule(schedule.id)).last_run_at == clock()
        sleeping.clear()
        # Wake also recomputes deadlines after an edit or deletion.
        await store.delete_schedule(schedule.id)
        scheduler.wake()
        await asyncio.wait_for(sleeping.wait(), timeout=1)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1)


async def test_run_forever_sleeps_until_due_time(store, monkeypatch):
    schedule = Schedule("soon", "p", "every 1h", next_run_at=utcnow() + timedelta(seconds=0.05))
    await store.create_schedule(schedule)
    fired = asyncio.Event()
    original = store.fire_schedule

    async def fire(*args):
        result = await original(*args)
        fired.set()
        return result

    monkeypatch.setattr(store, "fire_schedule", fire)
    scheduler = Scheduler(store, max_sleep_seconds=3600)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_forever(stop))
    try:
        await asyncio.wait_for(fired.wait(), timeout=1)
        assert (await store.get_schedule(schedule.id)).last_run_at == schedule.next_run_at
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1)


async def test_run_forever_caps_sleep(store, clock, monkeypatch):
    # Observe the timeout passed to asyncio.wait without waiting five minutes.
    stop = asyncio.Event()
    observed = []
    real_wait = asyncio.wait

    async def wait(tasks, *, timeout, return_when):
        observed.append(timeout)
        stop.set()
        return await real_wait(tasks, timeout=1, return_when=return_when)

    monkeypatch.setattr(asyncio, "wait", wait)
    await store.create_schedule(Schedule("later", "p", "every 1d", next_run_at=clock() + timedelta(days=1)))
    await Scheduler(store, clock=clock, max_sleep_seconds=300).run_forever(stop)
    assert observed == [300]


async def test_run_forever_does_not_short_poll(store, clock, monkeypatch):
    tick = AsyncMock(return_value=0)
    scheduler = Scheduler(store, clock=clock, max_sleep_seconds=3600)
    monkeypatch.setattr(scheduler, "tick", tick)
    queried = asyncio.Event()

    async def next_due_at():
        queried.set()
        return None

    monkeypatch.setattr(store, "next_due_at", next_due_at)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_forever(stop))
    try:
        await asyncio.wait_for(queried.wait(), timeout=1)
        await asyncio.sleep(0.03)
        assert tick.await_count == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1)


async def test_run_forever_retains_wake_during_database_read(store, clock, monkeypatch):
    stop = asyncio.Event()
    scheduler = Scheduler(store, clock=clock, max_sleep_seconds=3600)
    calls = 0

    async def next_due_at():
        nonlocal calls
        calls += 1
        if calls == 1:
            # A create/edit wakes us after this query has read its old snapshot.
            scheduler.wake()
        else:
            stop.set()
        return None

    monkeypatch.setattr(store, "next_due_at", next_due_at)
    await asyncio.wait_for(scheduler.run_forever(stop), timeout=1)
    assert calls == 2


async def test_run_forever_already_stopped(store, clock):
    stop = asyncio.Event()
    stop.set()
    scheduler = Scheduler(store, clock=clock)
    scheduler.tick = AsyncMock()
    await scheduler.run_forever(stop)
    scheduler.tick.assert_not_awaited()


async def test_run_forever_cancellation_cleans_waiters(store, clock, monkeypatch):
    class ObservedEvent(asyncio.Event):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.finished = False

        async def wait(self):
            self.entered.set()
            try:
                return await super().wait()
            finally:
                self.finished = True

    scheduler = Scheduler(store, clock=clock, max_sleep_seconds=3600)
    scheduler._wake = ObservedEvent()
    stop = ObservedEvent()
    task = asyncio.create_task(scheduler.run_forever(stop))
    await asyncio.wait_for(scheduler._wake.entered.wait(), timeout=1)
    await asyncio.wait_for(stop.entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scheduler._wake.finished and stop.finished


def test_max_sleep_must_be_positive(store):
    with pytest.raises(ValueError, match="greater than zero"):
        Scheduler(store, max_sleep_seconds=0)
