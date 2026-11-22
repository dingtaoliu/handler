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
