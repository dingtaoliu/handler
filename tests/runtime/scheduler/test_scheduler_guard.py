from datetime import datetime, timedelta, timezone

from handler.runtime.scheduler import Scheduler
from handler.runtime.store.sqlite import SQLiteCore
from handler.runtime.store.sqlite_schedules import ScheduleMixin
from handler.runtime.types import Schedule


class _Store(ScheduleMixin, SQLiteCore):
    pass


async def test_invalid_stored_spec_is_disabled_without_stopping_others(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = _Store(tmp_path / "s.db", clock=lambda: now)
    bad = Schedule(name="bad", prompt="x", spec="every nope", next_run_at=now - timedelta(minutes=1))
    good = Schedule(name="good", prompt="y", spec="every 1h", next_run_at=now - timedelta(minutes=1))
    await store.create_schedule(bad)
    await store.create_schedule(good)
    fired = await Scheduler(store, clock=lambda: now).tick()
    assert fired == 1
    assert (await store.get_schedule(bad.id)).enabled is False
    await store.close()
