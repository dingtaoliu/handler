import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import Lane, Message, Role, Run, RunStatus, RunStep, StepKind, TokenUsage


async def test_data_survives_close_and_reopen(tmp_path, clock, spec, event):
    path = tmp_path / "persistent.sqlite"
    original = SQLiteStore(str(path), clock=clock)
    queued = event("first")
    try:
        await original.put(queued, spec)
        lease, = await original.claim("worker", spec.lane, 1, 60)
        run = Run(
            "run", spec.key, lease.version, [queued.id], RunStatus.RUNNING,
            spec.harness, spec.model, started_at=clock(), usage=TokenUsage(10, 3, 2, 4, 1),
        )
        message = Message(spec.key, Role.ASSISTANT, [], run_id=run.id, created_at=clock())
        step = RunStep(run.id, 1, StepKind.ASSISTANT_TEXT, "answer", "stream", started_at=clock())
        assert await original.create_run(lease, run)
        assert await original.set_session(lease, "session")
        assert await original.add_messages(lease, [message])
        await original.add_step(step)
        finished = replace(run, status=RunStatus.COMPLETED, finished_at=clock(), cost_usd=0.02)
        assert await original.finish_run(lease, finished)
        assert await original.complete(lease, run.id, [queued.id])
        thread = await original.get_thread(spec.key)
    finally:
        await original.close()
    reopened = SQLiteStore(path, clock=clock)
    try:
        assert await reopened.get_thread(spec.key) == thread
        assert await reopened.get_run(run.id) == finished
        assert await reopened.list_messages(spec.key) == [message]
        assert await reopened.list_steps(run.id) == [step]
        assert await reopened.pending_events(spec.key) == []
        assert not await reopened.put(queued, spec)
        assert await reopened.claim("new", spec.lane, 10, 60) == []
    finally:
        await reopened.close()


async def test_sqlite_configuration_schema_and_storage_formats(store, tmp_path, running):
    lease, run = running
    settings = await store._call(lambda db: {
        name: db.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ["journal_mode", "foreign_keys", "busy_timeout"]
    })
    assert settings["journal_mode"] == "wal"
    assert settings["foreign_keys"] == 1 and settings["busy_timeout"] > 0
    with sqlite3.connect(tmp_path / "store.sqlite") as db:
        event = db.execute("SELECT payload, received_at, status FROM events").fetchone()
        stored_run = db.execute("SELECT usage, trigger_event_ids, status FROM runs").fetchone()
        indexes = dict(db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index'"))
    assert json.loads(event[0]) == {"text": "trigger"}
    assert event[1].endswith("+00:00") and event[2] == "routed"
    assert json.loads(stored_run[0]) == {
        "input_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0,
        "output_tokens": 0, "reasoning_tokens": 0,
    }
    assert json.loads(stored_run[1]) == run.trigger_event_ids
    assert stored_run[2] == "running"
    assert "WHERE pending_since IS NOT NULL" in indexes["threads_pending"]
    assert "events(thread_key, consumed_by_run)" in indexes["events_pending"]


async def test_in_memory_store_and_idempotent_close(clock, spec, event):
    store = SQLiteStore(":memory:", clock=clock)
    assert await store.put(event("first"), spec)
    assert len(await store.pending_events(spec.key)) == 1
    await asyncio.gather(store.close(), store.close())
    await store.close()
    with pytest.raises(RuntimeError, match="closed"):
        await store.get_thread(spec.key)
    unopened = SQLiteStore(":memory:")
    await unopened.close()


async def test_put_rolls_back_event_if_thread_write_fails(store, tmp_path, spec, event):
    assert await store.list_threads() == []
    with sqlite3.connect(tmp_path / "store.sqlite") as db:
        db.execute(
            "CREATE TRIGGER reject_thread BEFORE INSERT ON threads "
            "WHEN NEW.key = 'broken' BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
    queued = event("first")
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        await store.put(queued, replace(spec, key="broken"))
    assert await store.get_thread("broken") is None
    assert await store.put(queued, spec)
    assert await store.pending_events(spec.key) == [queued]


async def test_all_db_work_uses_one_worker_and_does_not_block_event_loop(
    tmp_path, clock, spec, event, monkeypatch,
):
    original_connect = sqlite3.connect
    calls = []
    started = threading.Event()
    release = threading.Event()

    class TrackedConnection(sqlite3.Connection):
        def execute(self, query, *args, **kwargs):
            calls.append(("execute", threading.get_ident()))
            if query == "SELECT * FROM threads WHERE key = ?":
                started.set()
                if not release.wait(5):
                    raise RuntimeError("test failed to release DB worker")
            return super().execute(query, *args, **kwargs)

        def executemany(self, *args, **kwargs):
            calls.append(("executemany", threading.get_ident()))
            return super().executemany(*args, **kwargs)

        def executescript(self, *args, **kwargs):
            calls.append(("executescript", threading.get_ident()))
            return super().executescript(*args, **kwargs)

        def commit(self):
            calls.append(("commit", threading.get_ident()))
            return super().commit()

        def rollback(self):
            calls.append(("rollback", threading.get_ident()))
            return super().rollback()

        def close(self):
            calls.append(("close", threading.get_ident()))
            return super().close()

    def connect(*args, **kwargs):
        calls.append(("connect", threading.get_ident()))
        return original_connect(*args, factory=TrackedConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    store = SQLiteStore(tmp_path / "worker.sqlite", clock=clock)
    try:
        await asyncio.gather(*(store.put(event(str(i)), spec) for i in range(10)))
        lease, = await store.claim("worker", spec.lane, 1, 60)
        pending = asyncio.create_task(store.get_thread(spec.key))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await asyncio.sleep(0)
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).key == spec.key
        assert await store.complete(lease, "run", [])
    finally:
        await store.close()
    assert {name for name, _ in calls} >= {
        "connect", "execute", "executemany", "executescript", "commit", "close",
    }
    assert len({thread_id for _, thread_id in calls}) == 1
    assert calls[0][1] != threading.get_ident()


async def test_separate_connections_claim_atomically(tmp_path, clock, spec, event):
    path = tmp_path / "shared.sqlite"
    first = SQLiteStore(path, clock=clock)
    second = SQLiteStore(path, clock=clock)
    try:
        await first.put(event("first"), spec)
        claims = await asyncio.gather(
            first.claim("one", Lane.INTERACTIVE, 1, 60),
            second.claim("two", Lane.INTERACTIVE, 1, 60),
        )
        assert sorted(map(len, claims)) == [0, 1]
    finally:
        await asyncio.gather(first.close(), second.close())


async def test_heartbeat_renews_from_transaction_time(store, clock, running):
    lease, _ = running
    started = threading.Event()
    release = threading.Event()

    def busy_worker(db):
        started.set()
        if not release.wait(5):
            raise RuntimeError("test failed to release DB worker")

    busy = asyncio.create_task(store._call(busy_worker))
    renewal = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        renewal = asyncio.create_task(store.heartbeat(lease, 10))
        await asyncio.sleep(0)
        clock.advance(5)
    finally:
        release.set()
        await busy
    renewed = await renewal
    assert renewed.until == clock() + timedelta(seconds=10)
    assert (await store.get_thread(lease.thread_key)).lease_until == renewed.until
