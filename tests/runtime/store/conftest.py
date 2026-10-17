from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import Event, Lane, Run, RunStatus, ThreadSpec, ThreadType


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
    instance = SQLiteStore(tmp_path / "store.sqlite", clock=clock)
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
def spec():
    return ThreadSpec(
        "chat:u:1", ThreadType.CHAT, Lane.INTERACTIVE,
        owner_user_id="u", harness="codex", model="test-model", priority=2,
    )


@pytest.fixture
def event(clock, spec):
    def make(key: str, **kwargs) -> Event:
        values = {
            "type": "chat.message", "source": "test", "payload": {"text": key},
            "idempotency_key": key, "thread_key": spec.key, "user_id": "u",
            "received_at": clock(),
        }
        values.update(kwargs)
        return Event(**values)

    return make


@pytest.fixture
async def running(store, clock, spec, event):
    queued = event("trigger")
    await store.put(queued, spec)
    lease, = await store.claim("worker", spec.lane, 1, 60)
    run = Run(
        "run", spec.key, lease.version, [queued.id], RunStatus.RUNNING,
        spec.harness, spec.model, started_at=clock(),
    )
    assert await store.create_run(lease, run)
    return lease, run
