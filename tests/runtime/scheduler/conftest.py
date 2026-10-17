from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from handler.runtime.store.sqlite import SQLiteCore
from handler.runtime.store.sqlite_schedules import ScheduleMixin


class _Store(ScheduleMixin, SQLiteCore):
    pass


@dataclass
class Clock:
    now: datetime = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def store(tmp_path, clock):
    instance = _Store(tmp_path / "store.sqlite", clock=clock)
    try:
        yield instance
    finally:
        await instance.close()
