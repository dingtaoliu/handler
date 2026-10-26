"""SQLite persistence, serialized on a single dedicated worker thread."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from .tasks import announce_routed
from ..types import (
    ABANDONED_ERROR,
    Event,
    EventStatus,
    Lane,
    Lease,
    Message,
    Role,
    Run,
    RunStatus,
    RunStep,
    StepKind,
    Thread,
    ThreadSpec,
    ThreadType,
    TokenUsage,
    utcnow,
)

T = TypeVar("T")
_JSON_FIELDS = {"payload", "blocks", "raw", "data", "usage", "trigger_event_ids"}
_TIME_FIELDS = {
    "received_at", "created_at", "pending_since", "available_at", "lease_until",
    "started_at", "finished_at", "ended_at",
}


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _values(record: Any) -> dict[str, Any]:
    result = asdict(record)
    for name, value in result.items():
        if value is None:
            continue
        if name in _JSON_FIELDS:
            result[name] = json.dumps(value)
        elif isinstance(value, datetime):
            result[name] = _timestamp(value)
        elif isinstance(value, Enum):
            result[name] = value.value
    return result


def _record(cls: type[T], row: sqlite3.Row) -> T:
    values = {field.name: row[field.name] for field in fields(cls)}
    for name, value in values.items():
        if value is None:
            continue
        if name in _JSON_FIELDS:
            values[name] = json.loads(value)
        elif name in _TIME_FIELDS:
            values[name] = datetime.fromisoformat(value)
    if cls is Thread:
        values["type"] = ThreadType(values["type"])
        values["lane"] = Lane(values["lane"])
        values["dead"] = bool(values["dead"])
    elif cls is Run:
        values["status"] = RunStatus(values["status"])
    elif cls is Message:
        values["role"] = Role(values["role"])
    elif cls is RunStep:
        values["kind"] = StepKind(values["kind"])
    if "usage" in values and values["usage"] is not None:
        values["usage"] = TokenUsage(**values["usage"])
    return cls(**values)


def _insert(db: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)


@contextmanager
def _transaction(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


class SQLiteCore:
    """Core tables (events, threads, runs, messages, steps) and DB plumbing.

    Domain mixins (schedules, approvals, memory, ...) live in sqlite_<domain>.py,
    use self._call / self._clock / self._wake and the module helpers below, and
    ship their own schema_<domain>.sql. SQLiteStore composes them.
    """
    def __init__(
        self, path: str | Path, *, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._path = str(path)
        self._clock = clock
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="handler-store")
        self._db: sqlite3.Connection | None = None
        self._wake = asyncio.Event()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    def _execute(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        if self._db is None:
            db = sqlite3.connect(self._path, isolation_level=None)
            try:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA busy_timeout = 5000")
                db.execute("PRAGMA journal_mode = WAL")
                db.execute("PRAGMA foreign_keys = ON")
                for schema in sorted(Path(__file__).parent.glob("schema*.sql")):
                    if schema.name.startswith("schema_pg"):
                        continue
                    db.executescript(schema.read_text())
            except BaseException:
                db.close()
                raise
            self._db = db
        return operation(self._db)

    async def _call(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        if self._closed:
            raise RuntimeError("SQLiteStore is closed")
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, self._execute, operation
        )

    async def _list(
        self, cls: type[T], query: str, parameters: tuple[Any, ...]
    ) -> list[T]:
        return await self._call(
            lambda db: [_record(cls, row) for row in db.execute(query, parameters)]
        )

    async def _fenced(
        self, lease: Lease, operation: Callable[[sqlite3.Connection], bool]
    ) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            with _transaction(db):
                held = db.execute(
                    "SELECT 1 FROM threads WHERE key = ? "
                    "AND lease_owner = ? AND lease_version = ?",
                    (lease.thread_key, lease.owner, lease.version),
                ).fetchone()
                return operation(db) if held else False

        return await self._call(write)

    async def put(self, event: Event, thread: ThreadSpec | None) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            with _transaction(db):
                values = _values(event)
                values["thread_key"] = thread.key if thread else event.thread_key
                values["status"] = (
                    EventStatus.ROUTED.value if thread else EventStatus.IGNORED.value
                )
                columns = ", ".join(values)
                placeholders = ", ".join(f":{name}" for name in values)
                inserted = db.execute(
                    f"INSERT INTO events ({columns}) VALUES ({placeholders}) "
                    "ON CONFLICT(idempotency_key) DO NOTHING", values,
                ).rowcount
                if not inserted:
                    return False
                if thread is not None:
                    now = _timestamp(self._clock())
                    # A new chat message revives a dead thread: the person is asking again.
                    revive = event.type == "chat.message"
                    db.execute(
                        "INSERT INTO threads "
                        "(key, type, lane, owner_user_id, harness, model, priority, "
                        "created_at, pending_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET "
                        "pending_since = COALESCE(threads.pending_since, excluded.pending_since), "
                        "dead = CASE WHEN ? THEN 0 ELSE threads.dead END, "
                        "attempts = CASE WHEN ? THEN 0 ELSE threads.attempts END",
                        (thread.key, thread.type.value, thread.lane.value,
                         thread.owner_user_id, thread.harness, thread.model,
                         thread.priority, now, now, revive, revive),
                    )
                return True

        inserted = await self._call(write)
        if inserted:
            self._wake.set()
            announce_routed(self, event, thread)
        return inserted

    async def claim(
        self, owner: str, lane: Lane, limit: int, lease_seconds: float
    ) -> list[Lease]:
        def write(db: sqlite3.Connection) -> list[Lease]:
            if limit <= 0:
                return []
            with _transaction(db):
                now = self._clock()
                until = now + timedelta(seconds=lease_seconds)
                rows = db.execute(
                    "SELECT key, lease_version FROM threads "
                    "WHERE lane = ? AND pending_since IS NOT NULL AND dead = 0 "
                    "AND (available_at IS NULL OR available_at <= ?) "
                    "AND (lease_until IS NULL OR lease_until < ?) "
                    "ORDER BY priority, pending_since, key LIMIT ?",
                    (lane.value, _timestamp(now), _timestamp(now), limit),
                ).fetchall()
                leases = [Lease(row["key"], owner, row["lease_version"] + 1, until)
                          for row in rows]
                db.executemany(
                    "UPDATE threads SET lease_owner = ?, lease_until = ?, "
                    "lease_version = ? WHERE key = ?",
                    [(lease.owner, _timestamp(lease.until), lease.version, lease.thread_key)
                     for lease in leases],
                )
                return leases

        return await self._call(write)

    async def heartbeat(self, lease: Lease, lease_seconds: float) -> Lease | None:
        renewed: Lease | None = None

        def write(db: sqlite3.Connection) -> bool:
            nonlocal renewed
            until = self._clock() + timedelta(seconds=lease_seconds)
            db.execute("UPDATE threads SET lease_until = ? WHERE key = ?",
                       (_timestamp(until), lease.thread_key))
            renewed = Lease(lease.thread_key, lease.owner, lease.version, until)
            return True

        await self._fenced(lease, write)
        return renewed

    async def pending_events(self, thread_key: str) -> list[Event]:
        return await self._list(
            Event, "SELECT * FROM events WHERE thread_key = ? AND status = 'routed' "
            "AND consumed_by_run IS NULL ORDER BY received_at, rowid", (thread_key,),
        )

    async def complete(self, lease: Lease, run_id: str, event_ids: list[str]) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            db.executemany(
                "UPDATE events SET consumed_by_run = ? WHERE id = ? "
                "AND thread_key = ? AND status = 'routed' AND consumed_by_run IS NULL",
                [(run_id, event_id, lease.thread_key) for event_id in event_ids],
            )
            pending = db.execute(
                "SELECT 1 FROM events WHERE thread_key = ? AND status = 'routed' "
                "AND consumed_by_run IS NULL LIMIT 1", (lease.thread_key,),
            ).fetchone()
            db.execute(
                "UPDATE threads SET pending_since = CASE WHEN ? THEN pending_since "
                "ELSE NULL END, attempts = 0, last_error = NULL, available_at = NULL, "
                "lease_owner = NULL, lease_until = NULL WHERE key = ?",
                (bool(pending), lease.thread_key),
            )
            return True

        completed = await self._fenced(lease, write)
        if completed:
            self._wake.set()
        return completed

    async def fail(
        self, lease: Lease, error: str, retry_in_seconds: float | None
    ) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            available_at = (
                _timestamp(self._clock() + timedelta(seconds=retry_in_seconds))
                if retry_in_seconds is not None else None
            )
            db.execute(
                "UPDATE threads SET attempts = attempts + 1, last_error = ?, "
                "dead = ?, available_at = ?, lease_owner = NULL, lease_until = NULL "
                "WHERE key = ?",
                (error, retry_in_seconds is None, available_at, lease.thread_key),
            )
            return True

        failed = await self._fenced(lease, write)
        if failed:
            self._wake.set()
        return failed

    async def wait_for_work(self, timeout: float) -> None:
        if self._wake.is_set():
            self._wake.clear()
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except TimeoutError:
            return
        self._wake.clear()

    async def list_events(
        self, *, source: str | None = None, status: str | None = None, limit: int = 100,
    ) -> list[tuple[Event, str]]:
        clauses, params = [], []
        for column, value in (("source", source), ("status", status)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""

        def read(db: sqlite3.Connection) -> list[tuple[Event, str]]:
            rows = db.execute(f"SELECT * FROM events {where}"
                              "ORDER BY received_at DESC, rowid DESC LIMIT ?",
                              (*params, limit)).fetchall()
            return [(_record(Event, row), row["status"]) for row in rows]

        return await self._call(read)

    async def get_thread(self, key: str) -> Thread | None:
        rows = await self._list(Thread, "SELECT * FROM threads WHERE key = ?", (key,))
        return rows[0] if rows else None

    async def list_threads(
        self, owner_user_id: str | None = None, limit: int = 100
    ) -> list[Thread]:
        where = "" if owner_user_id is None else "WHERE owner_user_id = ? "
        parameters = (limit,) if owner_user_id is None else (owner_user_id, limit)
        return await self._list(
            Thread, f"SELECT * FROM threads {where}ORDER BY COALESCE((SELECT MAX(created_at) FROM messages WHERE thread_key=threads.key), threads.created_at) DESC, rowid DESC LIMIT ?",
            parameters,
        )

    async def set_session(self, lease: Lease, session_id: str) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            db.execute("UPDATE threads SET session_id = ? WHERE key = ?",
                       (session_id, lease.thread_key))
            return True

        return await self._fenced(lease, write)

    async def create_run(self, lease: Lease, run: Run) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            if run.thread_key != lease.thread_key or run.lease_version != lease.version:
                return False
            values = _values(run)
            values["status"] = RunStatus.RUNNING.value
            _insert(db, "runs", values)
            return True

        return await self._fenced(lease, write)

    async def finish_run(self, lease: Lease, run: Run) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            if run.thread_key != lease.thread_key or run.lease_version != lease.version:
                return False
            values = _values(run)
            return bool(db.execute(
                "UPDATE runs SET status = :status, finished_at = :finished_at, "
                "error = :error, usage = :usage, cost_usd = :cost_usd "
                "WHERE id = :id AND thread_key = :thread_key AND lease_version = :lease_version",
                values,
            ).rowcount)

        return await self._fenced(lease, write)

    async def abandon_run(self, run_id: str, error: str) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            return bool(db.execute(
                "UPDATE runs SET status = ?, finished_at = ?, error = COALESCE(error, ?) "
                "WHERE id = ? AND status = ?",
                (RunStatus.ABANDONED.value, _timestamp(self._clock()), error[:2000], run_id,
                 RunStatus.RUNNING.value),
            ).rowcount)

        return await self._call(write)

    async def sweep_abandoned_runs(self, thread_key: str | None = None) -> list[str]:
        def write(db: sqlite3.Connection) -> list[str]:
            now = _timestamp(self._clock())
            where = "" if thread_key is None else "AND r.thread_key = ? "
            with _transaction(db):
                ids = [row["id"] for row in db.execute(
                    "SELECT r.id FROM runs r JOIN threads t ON t.key = r.thread_key "
                    "WHERE r.status = ? " + where +
                    "AND NOT (t.lease_version = r.lease_version AND t.lease_until IS NOT NULL "
                    "AND t.lease_until > ?)",
                    (RunStatus.RUNNING.value,
                     *((thread_key,) if thread_key is not None else ()), now),
                )]
                db.executemany(
                    "UPDATE runs SET status = ?, finished_at = ?, error = COALESCE(error, ?) "
                    "WHERE id = ? AND status = ?",
                    [(RunStatus.ABANDONED.value, now, ABANDONED_ERROR, run_id,
                      RunStatus.RUNNING.value) for run_id in ids],
                )
            return ids

        return await self._call(write)

    async def revive_thread(self, key: str, *, reset_session: bool = False,
                            discard_pending: bool = False) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            session = ", session_id = NULL" if reset_session else ""
            with _transaction(db):
                if not db.execute(
                    "UPDATE threads SET dead = 0, attempts = 0, last_error = NULL, "
                    f"available_at = NULL{session} WHERE key = ?", (key,),
                ).rowcount:
                    return False
                if discard_pending:
                    db.execute(
                        "UPDATE events SET consumed_by_run = 'revive' WHERE thread_key = ? "
                        "AND status = 'routed' AND consumed_by_run IS NULL", (key,))
                    db.execute("UPDATE threads SET pending_since = NULL WHERE key = ?", (key,))
            return True

        revived = await self._call(write)
        if revived:
            self._wake.set()
        return revived

    async def get_run(self, run_id: str) -> Run | None:
        rows = await self._list(Run, "SELECT * FROM runs WHERE id = ?", (run_id,))
        return rows[0] if rows else None

    async def list_runs(self, thread_key: str, limit: int = 50) -> list[Run]:
        return await self._list(
            Run, "SELECT * FROM runs WHERE thread_key = ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT ?", (thread_key, limit),
        )

    async def add_messages(self, lease: Lease, messages: list[Message]) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            if any(message.thread_key != lease.thread_key for message in messages):
                return False
            for message in messages:
                _insert(db, "messages", _values(message))
            return True

        return await self._fenced(lease, write)

    async def list_messages(
        self, thread_key: str, limit: int = 200, before: datetime | None = None,
        *, newest: bool = False,
    ) -> list[Message]:
        where = "thread_key = ?"
        parameters: tuple[Any, ...] = (thread_key,)
        if before is not None:
            where += " AND created_at < ?"
            parameters += (_timestamp(before),)
        direction = "DESC" if newest else "ASC"
        rows = await self._list(
            Message, f"SELECT * FROM messages WHERE {where} "
            f"ORDER BY created_at {direction}, rowid {direction} LIMIT ?", parameters + (limit,),
        )
        return list(reversed(rows)) if newest else rows

    async def add_step(self, step: RunStep) -> None:
        await self._call(lambda db: _insert(db, "run_steps", _values(step)))

    async def update_step(self, step: RunStep) -> None:
        def write(db: sqlite3.Connection) -> None:
            db.execute(
                "UPDATE run_steps SET ended_at = :ended_at, status = :status, "
                "usage = :usage, cost_usd = :cost_usd, data = :data "
                "WHERE run_id = :run_id AND seq = :seq", _values(step),
            )

        await self._call(write)

    async def list_steps(self, run_id: str) -> list[RunStep]:
        return await self._list(
            RunStep, "SELECT * FROM run_steps WHERE run_id = ? ORDER BY seq", (run_id,),
        )

    def _close_connection(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    async def _shutdown(self) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._executor, self._close_connection
            )
        finally:
            self._executor.shutdown(wait=False)

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._close_task)


# Domain mixins import the helpers above, so they're imported after them.
from .sqlite_connections import ConnectionMixin  # noqa: E402
from .sqlite_gateway import ApprovalMixin, AuditMixin  # noqa: E402
from .sqlite_memory import MemoryMixin, NotificationMixin, SettingsMixin  # noqa: E402
from .sqlite_schedules import ScheduleMixin  # noqa: E402
from .sqlite_users import UsersMixin  # noqa: E402


from .deliveries import SQLiteDeliveries  # noqa: E402
from .tasks import TaskQueries  # noqa: E402
from .thread_metadata import SQLiteThreadMetadata  # noqa: E402


class SQLiteStore(
    TaskQueries, SQLiteThreadMetadata, SQLiteDeliveries, ScheduleMixin, ApprovalMixin,
    AuditMixin,
    MemoryMixin, SettingsMixin,
    NotificationMixin, ConnectionMixin, UsersMixin, SQLiteCore,
):
    """The full SQLite store: core plus every domain mixin."""
