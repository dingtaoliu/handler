import asyncio
import sqlite3
from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from handler.runtime.types import Event, Lane, Schedule, ThreadSpec, ThreadType

from .conftest import _Store


def firing(schedule, clock, **event_args):
    key = f"automation:{schedule.id}"
    event = Event(
        type="automation.fired", source="scheduler", payload={"schedule_id": schedule.id},
        idempotency_key=f"automation:{schedule.id}:{schedule.next_run_at.isoformat()}",
        thread_key=key, received_at=clock(), **event_args,
    )
    thread = ThreadSpec(key, ThreadType.AUTOMATION, Lane.BACKGROUND, owner_user_id="u")
    return event, thread


async def test_crud_and_round_trip(store, clock):
    assert await store.get_schedule("missing") is None
    assert await store.list_schedules() == []
    assert await store.next_due_at() is None
    schedule = Schedule(
        "Daily digest", "Summarize", "cron: 0 7 * * *", created_by="u",
        timezone="America/New_York", notify=["u", "other"], harness="test", model="m",
        parent_thread_key="chat:u:1", next_run_at=clock(),
        last_run_at=clock() - timedelta(days=1), created_at=clock(),
    )
    await store.create_schedule(schedule)
    assert await store.get_schedule(schedule.id) == schedule
    assert await store.list_schedules() == [schedule]
    stored = await store.get_schedule(schedule.id)
    assert stored.enabled is True and stored.one_shot is False
    changed = replace(schedule, name="Renamed", enabled=False, one_shot=True,
                      notify=[], prompt="New prompt", next_run_at=None, last_run_at=None)
    await store.update_schedule(changed)
    assert await store.get_schedule(schedule.id) == changed
    assert await store.list_schedules() == []
    assert await store.list_schedules(include_disabled=True) == [changed]
    assert await store.next_due_at() is None
    assert await store.delete_schedule(schedule.id)
    assert not await store.delete_schedule(schedule.id)
    assert await store.get_schedule(schedule.id) is None


async def test_due_ordering_limits_and_utc_normalization(store, clock):
    schedules = [
        Schedule("future", "", "every 1h", next_run_at=clock() + timedelta(hours=1)),
        Schedule("now", "", "every 1h", next_run_at=clock()),
        Schedule("old", "", "every 1h", next_run_at=(clock() - timedelta(hours=1)).astimezone(
            timezone(timedelta(hours=3)))),
        Schedule("disabled", "", "every 1h", next_run_at=clock() - timedelta(days=1), enabled=False),
        Schedule("unscheduled", "", "every 1h"),
    ]
    for schedule in schedules:
        await store.create_schedule(schedule)
    assert await store.next_due_at() == clock() - timedelta(hours=1)
    assert [s.name for s in await store.due_schedules(clock())] == ["old", "now"]
    assert [s.name for s in await store.due_schedules(clock(), limit=1)] == ["old"]
    assert await store.due_schedules(clock(), limit=0) == []
    assert len(await store.due_schedules(clock() + timedelta(hours=1))) == 3
    assert (await store.get_schedule(schedules[2].id)).next_run_at.tzinfo == timezone.utc


@pytest.mark.parametrize("separate_connections", [False, True])
async def test_concurrent_fire_compare_and_set(store, clock, separate_connections):
    schedule = Schedule("s", "p", "every 1h", next_run_at=clock())
    await store.create_schedule(schedule)
    other = _Store(store._path, clock=clock) if separate_connections else store
    try:
        event, thread = firing(schedule, clock)
        next_run = clock() + timedelta(hours=1)
        outcomes = await asyncio.gather(*[
            instance.fire_schedule(schedule, clock(), next_run, replace(event, id=f"evt_{i}"), thread)
            for i, instance in enumerate((store, other))
        ])
        assert sorted(outcomes) == [False, True]
        assert len(await store.pending_events(thread.key)) == 1
        stored = await store.get_schedule(schedule.id)
        assert stored.last_run_at == clock()
        assert stored.next_run_at == next_run
        assert stored.enabled is True
        routed = await store.get_thread(thread.key)
        assert routed.type == ThreadType.AUTOMATION
        assert routed.lane == Lane.BACKGROUND
        assert routed.owner_user_id == "u"
        assert routed.pending_since == clock()
        assert store._wake.is_set()
    finally:
        if other is not store:
            await other.close()


async def test_fire_idempotency_and_existing_thread_preserved(store, clock):
    schedule = Schedule("s", "p", "every 1h", next_run_at=clock())
    await store.create_schedule(schedule)
    event, thread = firing(schedule, clock)
    original = replace(thread, harness="original", model="original-model", priority=3)
    assert await store.put(event, original)
    clock.advance(60)
    assert await store.fire_schedule(schedule, schedule.next_run_at, clock(), event, thread)
    assert await store.pending_events(thread.key) == [event]
    routed = await store.get_thread(thread.key)
    assert routed.harness == "original" and routed.model == "original-model" and routed.priority == 3
    assert routed.pending_since == schedule.next_run_at
    assert (await store.get_schedule(schedule.id)).last_run_at == schedule.next_run_at


async def test_new_firing_keeps_existing_thread_settings(store, clock):
    schedule = Schedule("s", "p", "every 1h", next_run_at=clock())
    await store.create_schedule(schedule)
    first, thread = firing(schedule, clock)
    original = replace(thread, harness="original", model="original-model")
    await store.put(replace(first, id="evt_prior", idempotency_key="prior"), original)
    clock.advance(60)
    assert await store.fire_schedule(schedule, schedule.next_run_at, clock(), first, thread)
    routed = await store.get_thread(thread.key)
    assert routed.harness == "original" and routed.model == "original-model"
    assert routed.pending_since == schedule.next_run_at
    assert len(await store.pending_events(thread.key)) == 2


async def test_one_shot_disables_and_wakes_readers(store, clock):
    schedule = Schedule("s", "p", f"at: {clock().isoformat()}", one_shot=True, next_run_at=clock())
    await store.create_schedule(schedule)
    event, thread = firing(schedule, clock)
    waiter = asyncio.create_task(store.wait_for_work(3600))
    try:
        assert await store.fire_schedule(schedule, clock(), None, event, thread)
        await asyncio.wait_for(waiter, timeout=1)
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    stored = await store.get_schedule(schedule.id)
    assert stored.enabled is False and stored.next_run_at is None
    assert stored.last_run_at == clock()
    assert await store.due_schedules(clock()) == []
    assert await store.next_due_at() is None
    assert not await store.fire_schedule(schedule, clock(), None, event, thread)


async def test_stale_disabled_deleted_fire_does_not_emit(store, clock):
    schedule = Schedule("s", "p", "every 1h", next_run_at=clock())
    await store.create_schedule(schedule)
    event, thread = firing(schedule, clock)
    assert not await store.fire_schedule(schedule, clock() - timedelta(seconds=1), None, event, thread)
    await store.update_schedule(replace(schedule, enabled=False))
    assert not await store.fire_schedule(schedule, clock(), None, event, thread)
    await store.delete_schedule(schedule.id)
    assert not await store.fire_schedule(schedule, clock(), None, event, thread)
    assert await store.get_thread(thread.key) is None
    assert not store._wake.is_set()


async def test_firing_rolls_back_on_event_failure(store, clock):
    schedule = Schedule("s", "p", "every 1h", next_run_at=clock())
    await store.create_schedule(schedule)
    event, thread = firing(schedule, clock)
    await store.put(replace(event, idempotency_key="existing"), None)
    store._wake.clear()
    with pytest.raises(sqlite3.IntegrityError):
        await store.fire_schedule(schedule, clock(), None, event, thread)
    assert await store.get_schedule(schedule.id) == schedule
    assert await store.get_thread(thread.key) is None
    assert not store._wake.is_set()


async def test_firing_rolls_back_on_routing_failure(store, clock):
    schedule = Schedule("s", "p", "every 1h", next_run_at=clock())
    await store.create_schedule(schedule)
    event, thread = firing(schedule, clock)
    await store._call(lambda db: db.execute(
        "CREATE TRIGGER reject_thread BEFORE INSERT ON threads "
        "BEGIN SELECT RAISE(ABORT, 'routing failed'); END"
    ))
    with pytest.raises(sqlite3.IntegrityError, match="routing failed"):
        await store.fire_schedule(schedule, clock(), None, event, thread)
    assert await store.get_schedule(schedule.id) == schedule
    assert await store.pending_events(thread.key) == []
    assert not store._wake.is_set()
