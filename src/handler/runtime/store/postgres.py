"""Postgres persistence (psycopg 3 async pool); same semantics as SQLiteCore."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import asdict, fields
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from ..types import (
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
CHANNEL = "handler_work"
_JSON_FIELDS = {"payload", "blocks", "raw", "data", "usage", "trigger_event_ids"}
_SCHEMA_LOCK = 7_283_001


def _values(record: Any) -> dict[str, Any]:
    result = asdict(record)
    for name, value in result.items():
        if value is None:
            continue
        if name in _JSON_FIELDS:
            result[name] = Jsonb(value)
        elif isinstance(value, Enum):
            result[name] = value.value
    return result


def _record(cls: type[T], row: dict[str, Any]) -> T:
    values = {field.name: row[field.name] for field in fields(cls)}
    if cls is Thread:
        values["type"] = ThreadType(values["type"])
        values["lane"] = Lane(values["lane"])
    elif cls is Run:
        values["status"] = RunStatus(values["status"])
    elif cls is Message:
        values["role"] = Role(values["role"])
    elif cls is RunStep:
        values["kind"] = StepKind(values["kind"])
    if "usage" in values and values["usage"] is not None:
        values["usage"] = TokenUsage(**values["usage"])
    return cls(**values)


def _columns(cls: type) -> str:
    return ", ".join(field.name for field in fields(cls))


async def _insert(conn: AsyncConnection, table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join(f"%({name})s" for name in values)
    await conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)


async def _notify(conn: AsyncConnection) -> None:
    await conn.execute("SELECT pg_notify(%s, '')", (CHANNEL,))


async def _put_event(
    conn: AsyncConnection, event: Event, thread: ThreadSpec | None, now: datetime
) -> bool:
    """Insert an event idempotently and route its thread; False if a duplicate."""
    values = _values(event)
    values["thread_key"] = thread.key if thread else event.thread_key
    values["status"] = EventStatus.ROUTED.value if thread else EventStatus.IGNORED.value
    columns = ", ".join(values)
    placeholders = ", ".join(f"%({name})s" for name in values)
    cursor = await conn.execute(
        f"INSERT INTO events ({columns}) VALUES ({placeholders}) "
        "ON CONFLICT (idempotency_key) DO NOTHING", values,
    )
    if not cursor.rowcount:
        return False
    if thread is not None:
        await conn.execute(
            "INSERT INTO threads (key, type, lane, owner_user_id, harness, "
            "model, priority, created_at, pending_since) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (key) DO UPDATE SET "
            "pending_since = COALESCE(threads.pending_since, "
            "excluded.pending_since)",
            (thread.key, thread.type.value, thread.lane.value,
             thread.owner_user_id, thread.harness, thread.model,
             thread.priority, now, now),
        )
    return True


class PostgresCore:
    """Core tables (events, threads, runs, messages, steps) on Postgres.

    Domain mixins use self._conn() for a pooled transaction, self._clock and
    self._wake. The schema is applied at first use from schema_pg*.sql.
    """

    def __init__(
        self, dsn: str, *, clock: Callable[[], datetime] = utcnow,
        min_size: int = 1, max_size: int = 5,
    ) -> None:
        self._dsn = dsn
        self._clock = clock
        self._pool = AsyncConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=False,
            kwargs={"row_factory": dict_row, "options": "-c timezone=UTC"},
        )
        self._wake = asyncio.Event()
        self._ready: asyncio.Task[None] | None = None
        self._listener: asyncio.Task[None] | None = None
        self._listening = asyncio.Event()
        self._closed = False

    # --- plumbing ---------------------------------------------------------
    async def _setup(self) -> None:
        await self._pool.open()
        async with self._pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK,))
            for schema in sorted(Path(__file__).parent.glob("schema_pg*.sql")):
                await conn.execute(schema.read_text())
        self._listener = asyncio.create_task(self._listen())
        await self._listening.wait()

    async def _ensure(self) -> None:
        if self._closed:
            raise RuntimeError("PostgresCore is closed")
        if self._ready is None:
            self._ready = asyncio.create_task(self._setup())
        await asyncio.shield(self._ready)

    def _conn(self) -> contextlib.AbstractAsyncContextManager[AsyncConnection]:
        return self._connection()

    @contextlib.asynccontextmanager
    async def _connection(self):
        await self._ensure()
        async with self._pool.connection() as conn:
            yield conn

    async def _list(self, cls: type[T], query: str, parameters: Any = ()) -> list[T]:
        async with self._conn() as conn:
            cursor = await conn.execute(query, parameters)
            return [_record(cls, row) for row in await cursor.fetchall()]

    async def _listen(self) -> None:
        while not self._closed:
            try:
                conn = await AsyncConnection.connect(self._dsn, autocommit=True)
                async with conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    self._listening.set()
                    self._wake.set()
                    async for _ in conn.notifies():
                        self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._listening.set()
                await asyncio.sleep(1)

    async def _fenced(
        self, lease: Lease, operation: Callable[[AsyncConnection], Any]
    ) -> bool:
        async with self._conn() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM threads WHERE key = %s AND lease_owner = %s "
                "AND lease_version = %s FOR UPDATE",
                (lease.thread_key, lease.owner, lease.version),
            )
            if await cursor.fetchone() is None:
                return False
            return bool(await operation(conn))

    # --- queue ------------------------------------------------------------
    async def put(self, event: Event, thread: ThreadSpec | None) -> bool:
        async with self._conn() as conn:
            if not await _put_event(conn, event, thread, self._clock()):
                return False
            await _notify(conn)
        self._wake.set()
        return True

    async def claim(
        self, owner: str, lane: Lane, limit: int, lease_seconds: float
    ) -> list[Lease]:
        if limit <= 0:
            return []
        async with self._conn() as conn:
            now = self._clock()
            until = now + timedelta(seconds=lease_seconds)
            cursor = await conn.execute(
                "SELECT key, lease_version FROM threads "
                "WHERE lane = %s AND pending_since IS NOT NULL AND NOT dead "
                "AND (available_at IS NULL OR available_at <= %s) "
                "AND (lease_until IS NULL OR lease_until < %s) "
                "ORDER BY priority, pending_since, key LIMIT %s "
                "FOR UPDATE SKIP LOCKED",
                (lane.value, now, now, limit),
            )
            leases = [
                Lease(row["key"], owner, row["lease_version"] + 1, until)
                for row in await cursor.fetchall()
            ]
            for lease in leases:
                await conn.execute(
                    "UPDATE threads SET lease_owner = %s, lease_until = %s, "
                    "lease_version = %s WHERE key = %s",
                    (lease.owner, lease.until, lease.version, lease.thread_key),
                )
            return leases

    async def heartbeat(self, lease: Lease, lease_seconds: float) -> Lease | None:
        renewed: Lease | None = None

        async def write(conn: AsyncConnection) -> bool:
            nonlocal renewed
            until = self._clock() + timedelta(seconds=lease_seconds)
            await conn.execute("UPDATE threads SET lease_until = %s WHERE key = %s",
                               (until, lease.thread_key))
            renewed = Lease(lease.thread_key, lease.owner, lease.version, until)
            return True

        await self._fenced(lease, write)
        return renewed

    async def pending_events(self, thread_key: str) -> list[Event]:
        return await self._list(
            Event, f"SELECT {_columns(Event)} FROM events WHERE thread_key = %s "
            "AND status = 'routed' AND consumed_by_run IS NULL "
            "ORDER BY received_at, seq", (thread_key,),
        )

    async def complete(self, lease: Lease, run_id: str, event_ids: list[str]) -> bool:
        async def write(conn: AsyncConnection) -> bool:
            for event_id in event_ids:
                await conn.execute(
                    "UPDATE events SET consumed_by_run = %s WHERE id = %s "
                    "AND thread_key = %s AND status = 'routed' "
                    "AND consumed_by_run IS NULL",
                    (run_id, event_id, lease.thread_key),
                )
            cursor = await conn.execute(
                "SELECT 1 FROM events WHERE thread_key = %s AND status = 'routed' "
                "AND consumed_by_run IS NULL LIMIT 1", (lease.thread_key,),
            )
            pending = await cursor.fetchone() is not None
            await conn.execute(
                "UPDATE threads SET pending_since = CASE WHEN %s THEN pending_since "
                "ELSE NULL END, attempts = 0, last_error = NULL, available_at = NULL, "
                "lease_owner = NULL, lease_until = NULL WHERE key = %s",
                (pending, lease.thread_key),
            )
            await _notify(conn)
            return True

        completed = await self._fenced(lease, write)
        if completed:
            self._wake.set()
        return completed

    async def fail(
        self, lease: Lease, error: str, retry_in_seconds: float | None
    ) -> bool:
        async def write(conn: AsyncConnection) -> bool:
            available_at = (
                self._clock() + timedelta(seconds=retry_in_seconds)
                if retry_in_seconds is not None else None
            )
            await conn.execute(
                "UPDATE threads SET attempts = attempts + 1, last_error = %s, "
                "dead = %s, available_at = %s, lease_owner = NULL, "
                "lease_until = NULL WHERE key = %s",
                (error, retry_in_seconds is None, available_at, lease.thread_key),
            )
            await _notify(conn)
            return True

        failed = await self._fenced(lease, write)
        if failed:
            self._wake.set()
        return failed

    async def wait_for_work(self, timeout: float) -> None:
        await self._ensure()
        if self._wake.is_set():
            self._wake.clear()
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except TimeoutError:
            return
        self._wake.clear()

    # --- threads ----------------------------------------------------------
    async def get_thread(self, key: str) -> Thread | None:
        rows = await self._list(
            Thread, f"SELECT {_columns(Thread)} FROM threads WHERE key = %s", (key,),
        )
        return rows[0] if rows else None

    async def list_threads(
        self, owner_user_id: str | None = None, limit: int = 100
    ) -> list[Thread]:
        where = "" if owner_user_id is None else "WHERE owner_user_id = %s "
        parameters = (limit,) if owner_user_id is None else (owner_user_id, limit)
        return await self._list(
            Thread, f"SELECT {_columns(Thread)} FROM threads {where}"
            "ORDER BY created_at DESC, seq DESC LIMIT %s", parameters,
        )

    async def set_session(self, lease: Lease, session_id: str) -> bool:
        async def write(conn: AsyncConnection) -> bool:
            await conn.execute("UPDATE threads SET session_id = %s WHERE key = %s",
                               (session_id, lease.thread_key))
            return True

        return await self._fenced(lease, write)

    # --- runs -------------------------------------------------------------
    async def create_run(self, lease: Lease, run: Run) -> bool:
        async def write(conn: AsyncConnection) -> bool:
            if run.thread_key != lease.thread_key or run.lease_version != lease.version:
                return False
            values = _values(run)
            values["status"] = RunStatus.RUNNING.value
            await _insert(conn, "runs", values)
            return True

        return await self._fenced(lease, write)

    async def finish_run(self, lease: Lease, run: Run) -> bool:
        async def write(conn: AsyncConnection) -> bool:
            if run.thread_key != lease.thread_key or run.lease_version != lease.version:
                return False
            cursor = await conn.execute(
                "UPDATE runs SET status = %(status)s, finished_at = %(finished_at)s, "
                "error = %(error)s, usage = %(usage)s, cost_usd = %(cost_usd)s "
                "WHERE id = %(id)s AND thread_key = %(thread_key)s "
                "AND lease_version = %(lease_version)s", _values(run),
            )
            return bool(cursor.rowcount)

        return await self._fenced(lease, write)

    async def get_run(self, run_id: str) -> Run | None:
        rows = await self._list(
            Run, f"SELECT {_columns(Run)} FROM runs WHERE id = %s", (run_id,),
        )
        return rows[0] if rows else None

    async def list_runs(self, thread_key: str, limit: int = 50) -> list[Run]:
        return await self._list(
            Run, f"SELECT {_columns(Run)} FROM runs WHERE thread_key = %s "
            "ORDER BY started_at DESC, seq DESC LIMIT %s", (thread_key, limit),
        )

    # --- messages ---------------------------------------------------------
    async def add_messages(self, lease: Lease, messages: list[Message]) -> bool:
        async def write(conn: AsyncConnection) -> bool:
            if any(message.thread_key != lease.thread_key for message in messages):
                return False
            for message in messages:
                await _insert(conn, "messages", _values(message))
            return True

        return await self._fenced(lease, write)

    async def list_messages(
        self, thread_key: str, limit: int = 200, before: datetime | None = None
    ) -> list[Message]:
        where = "thread_key = %s"
        parameters: tuple[Any, ...] = (thread_key,)
        if before is not None:
            where += " AND created_at < %s"
            parameters += (before,)
        return await self._list(
            Message, f"SELECT {_columns(Message)} FROM messages WHERE {where} "
            "ORDER BY created_at, seq LIMIT %s", parameters + (limit,),
        )

    # --- steps ------------------------------------------------------------
    async def add_step(self, step: RunStep) -> None:
        async with self._conn() as conn:
            await _insert(conn, "run_steps", _values(step))

    async def update_step(self, step: RunStep) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE run_steps SET ended_at = %(ended_at)s, status = %(status)s, "
                "usage = %(usage)s, cost_usd = %(cost_usd)s, data = %(data)s "
                "WHERE run_id = %(run_id)s AND seq = %(seq)s", _values(step),
            )

    async def list_steps(self, run_id: str) -> list[RunStep]:
        return await self._list(
            RunStep, f"SELECT {_columns(RunStep)} FROM run_steps "
            "WHERE run_id = %s ORDER BY seq", (run_id,),
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener
        if self._ready is not None:
            with contextlib.suppress(Exception):
                await self._ready
            await self._pool.close()


from .postgres_domains import (  # noqa: E402
    ApprovalMixin,
    AuditMixin,
    ConnectionMixin,
    MemoryMixin,
    NotificationMixin,
    ScheduleMixin,
    SettingsMixin,
    UsersMixin,
)


class PostgresStore(
    ScheduleMixin, ApprovalMixin, AuditMixin, MemoryMixin, SettingsMixin,
    NotificationMixin, ConnectionMixin, UsersMixin, PostgresCore,
):
    """The full Postgres store: core plus every domain mixin."""
