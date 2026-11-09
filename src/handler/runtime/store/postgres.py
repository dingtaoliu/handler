"""Postgres persistence (psycopg 3 async pool); same semantics as SQLiteCore."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, fields
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .postgres_hub import CONTROL_SCHEMA, SCHEMA_LOCK, SHARED_CHANNEL, PostgresHub
from .usage import PostgresUsage
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
CHANNEL = "handler_work"
_JSON_FIELDS = {"payload", "blocks", "raw", "data", "usage", "trigger_event_ids"}
_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]{0,39}")


def strip_nul(value: Any) -> Any:
    """Drop NUL characters, which Postgres text and jsonb both reject.

    ("\\u0000 cannot be converted to text": e.g. a binary file read as text.)
    """
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [strip_nul(v) for v in value]
    return value


def _values(record: Any) -> dict[str, Any]:
    result = asdict(record)
    for name, value in result.items():
        if value is None:
            continue
        if name in _JSON_FIELDS:
            result[name] = Jsonb(strip_nul(value))
        elif isinstance(value, Enum):
            result[name] = value.value
        elif isinstance(value, str):
            result[name] = strip_nul(value)
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


async def earliest_work_at(conn: AsyncConnection, sinks: Sequence[str] | None) -> datetime | None:
    """When this schema next needs a runtime: queued work, schedules, outbox, expiries.

    `sinks` limits outbox rows to those a runtime can deliver (None: all of them).
    Approvals and questions are included because only a runtime expires them.
    """
    cursor = await conn.execute(
        "SELECT LEAST("
        "(SELECT MIN(GREATEST(COALESCE(available_at, pending_since), "
        " COALESCE(lease_until, pending_since))) FROM threads "
        " WHERE pending_since IS NOT NULL AND NOT dead), "
        "(SELECT MIN(next_run_at) FROM schedules WHERE enabled), "
        "(SELECT MIN(next_attempt_at) FROM deliveries WHERE status = 'pending' "
        " AND (%(all)s OR sink = ANY(%(sinks)s))), "
        "(SELECT MIN(expires_at) FROM approvals WHERE status = 'pending'), "
        "(SELECT MIN(expires_at) FROM questions WHERE status = 'pending')) AS due",
        {"all": sinks is None, "sinks": list(sinks or [])})
    return (await cursor.fetchone())["due"]


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
        # A new chat message revives a dead thread: the person is asking again.
        revive = event.type == "chat.message"
        await conn.execute(
            "INSERT INTO threads (key, type, lane, owner_user_id, harness, "
            "model, priority, created_at, pending_since) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (key) DO UPDATE SET "
            "pending_since = COALESCE(threads.pending_since, "
            "excluded.pending_since), "
            "dead = CASE WHEN %s THEN FALSE ELSE threads.dead END, "
            "attempts = CASE WHEN %s THEN 0 ELSE threads.attempts END",
            (thread.key, thread.type.value, thread.lane.value,
             thread.owner_user_id, thread.harness, thread.model,
             thread.priority, now, now, revive, revive),
        )
    return True


class PostgresCore(PostgresUsage):
    """Core tables (events, threads, runs, messages, steps) on Postgres.

    Domain mixins use self._conn() for a pooled transaction, self._clock and
    self._wake. The schema is applied at first use from schema_pg*.sql.
    """

    def __init__(
        self, dsn: str, *, clock: Callable[[], datetime] = utcnow,
        min_size: int = 1, max_size: int = 5, schema: str | None = None,
        hub: PostgresHub | None = None,
    ) -> None:
        if schema is not None and not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError(f"invalid schema name {schema!r}: use [a-z0-9_], max 40")
        self._dsn = dsn
        self._clock = clock
        self._schema = schema
        self._hub = hub
        # On a hub the transaction names its schema; no schema means "public", the
        # same reading instance_configs gives it when checking for shared schemas.
        self._tenant_schema = schema or "public"
        self._scope = sql.SQL("SET LOCAL search_path TO {}").format(
            sql.Identifier(self._tenant_schema)) if hub else None
        self._options = "-c timezone=UTC" + (
            f" -c search_path={schema}" if schema else "")
        self._pool = hub.pool if hub else AsyncConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=False,
            kwargs={"row_factory": dict_row, "options": self._options},
        )
        self._wake = asyncio.Event()
        self._ready: asyncio.Task[None] | None = None
        self._listener: asyncio.Task[None] | None = None
        self._listening = asyncio.Event()
        self._closed = False
        self._marks_wake = False  # a registry's tenant_state exists for a store on its own

    # --- plumbing ---------------------------------------------------------
    async def _setup(self) -> None:
        ddl = [schema.read_text()
               for schema in sorted(Path(__file__).parent.glob("schema_pg*.sql"))]
        if self._hub is not None:
            await self._hub.start()
            await self._apply_versioned("core", ddl)
            self._hub.attach(self._tenant_schema, self._wake)
            self._wake.set()
            return
        await self._pool.open()
        async with self._pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
            if self._schema:
                await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(self._schema)))
            for text in ddl:
                await conn.execute(text)
            cursor = await conn.execute(
                "SELECT to_regclass(%s) AS state", (f"{CONTROL_SCHEMA}.tenant_state",))
            self._marks_wake = (await cursor.fetchone())["state"] is not None
        self._listener = asyncio.create_task(self._listen())
        await self._listening.wait()

    async def _applied_version(self, conn: AsyncConnection, component: str) -> str | None:
        cursor = await conn.execute(
            sql.SQL("SELECT version FROM {}.tenant_schema_versions "
                    "WHERE schema_name = %s AND component = %s").format(
                sql.Identifier(CONTROL_SCHEMA)), (self._tenant_schema, component))
        row = await cursor.fetchone()
        return row["version"] if row else None

    async def _apply_versioned(self, component: str, ddl: Sequence[str]) -> None:
        """Apply this tenant's DDL once per version; the check is one SELECT.

        The version is the DDL's own hash, so changed SQL can't ship without a
        re-apply. Everything is idempotent, so a rollback to older SQL is safe too.
        """
        version = hashlib.sha256("\0".join(ddl).encode()).hexdigest()
        async with self._scoped() as conn:
            if await self._applied_version(conn, component) == version:
                return
        assert self._hub is not None
        async with self._hub.ddl_lock:
            async with self._scoped() as conn:
                await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
                if await self._applied_version(conn, component) == version:
                    return
                if self._schema:
                    await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        sql.Identifier(self._schema)))
                for text in ddl:
                    await conn.execute(text)
                await conn.execute(
                    sql.SQL("INSERT INTO {}.tenant_schema_versions "
                            "(schema_name, component, version) VALUES (%s, %s, %s) "
                            "ON CONFLICT (schema_name, component) DO UPDATE SET "
                            "version = excluded.version, applied_at = now()").format(
                        sql.Identifier(CONTROL_SCHEMA)),
                    (self._tenant_schema, component, version))

    async def _apply_schema(self, component: str, ddl: Sequence[str]) -> None:
        """Create a domain's own tables (e.g. an integration's) in this store's schema."""
        await self._ensure()
        if self._hub is not None:
            await self._apply_versioned(component, ddl)
            return
        async with self._conn() as conn:
            for text in ddl:
                await conn.execute(text)

    async def _ensure(self) -> None:
        if self._closed:
            raise RuntimeError("PostgresCore is closed")
        if self._ready is None:
            self._ready = asyncio.create_task(self._setup())
        await asyncio.shield(self._ready)

    def _conn(self) -> contextlib.AbstractAsyncContextManager[AsyncConnection]:
        return self._connection()

    @contextlib.asynccontextmanager
    async def _scoped(self):
        """A pooled transaction, scoped to this tenant before any other statement."""
        async with self._pool.connection() as conn:
            if self._scope is not None:
                await conn.execute(self._scope)
            yield conn

    @contextlib.asynccontextmanager
    async def _connection(self):
        await self._ensure()
        async with self._scoped() as conn:
            yield conn

    async def _notify(self, conn: AsyncConnection) -> None:
        """Wake this schema's listeners when the transaction commits.

        A store with its own connections also uses the shared channel, so a one-off
        CLI process reaches a server that listens through a hub.
        """
        if self._hub is not None:
            await conn.execute("SELECT pg_notify(%s, %s)", (SHARED_CHANNEL, self._tenant_schema))
            return
        await conn.execute("SELECT pg_notify(%s || '_' || current_schema(), ''), "
                           "pg_notify(%s, current_schema())", (CHANNEL, SHARED_CHANNEL))
        if self._marks_wake:
            # Commits with the work: the wake survives a NOTIFY nobody heard.
            await conn.execute(
                sql.SQL("UPDATE {0}.tenant_state s SET next_wake_at = LEAST(s.next_wake_at, now()) "
                        "FROM {0}.tenants t WHERE t.name = s.name AND t.schema_name = %s").format(
                    sql.Identifier(CONTROL_SCHEMA)), (self._tenant_schema,))

    async def _list(self, cls: type[T], query: str, parameters: Any = ()) -> list[T]:
        async with self._conn() as conn:
            cursor = await conn.execute(query, parameters)
            return [_record(cls, row) for row in await cursor.fetchall()]

    async def _listen(self) -> None:
        while not self._closed:
            try:
                conn = await AsyncConnection.connect(
                    self._dsn, autocommit=True, options=self._options)
                async with conn:
                    cursor = await conn.execute(
                        "SELECT %s || '_' || current_schema() AS channel", (CHANNEL,))
                    row = await cursor.fetchone()
                    await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(row[0])))
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
            await self._notify(conn)
        self._wake.set()
        announce_routed(self, event, thread)
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

    async def list_events(
        self, *, source: str | None = None, status: str | None = None, limit: int = 100,
    ) -> list[tuple[Event, str]]:
        clauses, params = [], []
        for column, value in (("source", source), ("status", status)):
            if value is not None:
                clauses.append(f"{column} = %s")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        async with self._conn() as conn:
            cursor = await conn.execute(
                f"SELECT {_columns(Event)}, status FROM events {where}"
                "ORDER BY received_at DESC, seq DESC LIMIT %s", (*params, limit))
            return [(_record(Event, row), row["status"]) for row in await cursor.fetchall()]

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
            await self._notify(conn)
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
                (strip_nul(error), retry_in_seconds is None, available_at, lease.thread_key),
            )
            await self._notify(conn)
            return True

        failed = await self._fenced(lease, write)
        if failed:
            self._wake.set()
        return failed

    async def earliest_work(self, sinks: Sequence[str] | None = None) -> datetime | None:
        async with self._conn() as conn:
            return await earliest_work_at(conn, sinks)

    async def has_claimable(self, lane: Lane) -> bool:
        async with self._conn() as conn:
            now = self._clock()
            cursor = await conn.execute(
                "SELECT EXISTS (SELECT 1 FROM threads WHERE lane = %s "
                "AND pending_since IS NOT NULL AND NOT dead "
                "AND (available_at IS NULL OR available_at <= %s) "
                "AND (lease_until IS NULL OR lease_until < %s)) AS found",
                (lane.value, now, now))
            return (await cursor.fetchone())["found"]

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
        self, owner_user_id: str | None = None, limit: int = 100,
        types: Sequence[str] | None = None, exclude_types: Sequence[str] | None = None,
    ) -> list[Thread]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if owner_user_id is not None:
            clauses.append("(owner_user_id = %s OR type <> 'chat')")
            parameters.append(owner_user_id)
        if types:
            clauses.append("type = ANY(%s)")
            parameters.append(list(types))
        if exclude_types:
            clauses.append("NOT (type = ANY(%s))")
            parameters.append(list(exclude_types))
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        parameters.append(limit)
        return await self._list(
            Thread, f"SELECT {_columns(Thread)} FROM threads {where}"
            "ORDER BY COALESCE((SELECT MAX(created_at) FROM messages WHERE thread_key=threads.key), threads.created_at) DESC, seq DESC LIMIT %s", tuple(parameters),
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

    async def abandon_run(self, run_id: str, error: str) -> bool:
        async with self._conn() as conn:
            cursor = await conn.execute(
                "UPDATE runs SET status = %s, finished_at = %s, "
                "error = COALESCE(error, %s) WHERE id = %s AND status = %s",
                (RunStatus.ABANDONED.value, self._clock(), strip_nul(error)[:2000], run_id,
                 RunStatus.RUNNING.value),
            )
            return bool(cursor.rowcount)

    async def sweep_abandoned_runs(self, thread_key: str | None = None) -> list[str]:
        now = self._clock()
        where = "" if thread_key is None else "AND r.thread_key = %s "
        async with self._conn() as conn:
            cursor = await conn.execute(
                "UPDATE runs r SET status = %s, finished_at = %s, "
                "error = COALESCE(r.error, %s) FROM threads t "
                "WHERE t.key = r.thread_key AND r.status = %s " + where +
                "AND NOT (t.lease_version = r.lease_version AND t.lease_until IS NOT NULL "
                "AND t.lease_until > %s) RETURNING r.id",
                (RunStatus.ABANDONED.value, now, ABANDONED_ERROR, RunStatus.RUNNING.value,
                 *((thread_key,) if thread_key is not None else ()), now),
            )
            return [row["id"] for row in await cursor.fetchall()]

    async def revive_thread(self, key: str, *, reset_session: bool = False,
                            discard_pending: bool = False) -> bool:
        async with self._conn() as conn:
            session = ", session_id = NULL" if reset_session else ""
            cursor = await conn.execute(
                "UPDATE threads SET dead = FALSE, attempts = 0, last_error = NULL, "
                f"available_at = NULL{session} WHERE key = %s", (key,))
            if not cursor.rowcount:
                return False
            if discard_pending:
                await conn.execute(
                    "UPDATE events SET consumed_by_run = 'revive' WHERE thread_key = %s "
                    "AND status = 'routed' AND consumed_by_run IS NULL", (key,))
                await conn.execute("UPDATE threads SET pending_since = NULL WHERE key = %s",
                                   (key,))
            await self._notify(conn)
        self._wake.set()
        return True

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
        self, thread_key: str, limit: int = 200, before: datetime | None = None,
        *, newest: bool = False,
    ) -> list[Message]:
        where = "thread_key = %s"
        parameters: tuple[Any, ...] = (thread_key,)
        if before is not None:
            where += " AND created_at < %s"
            parameters += (before,)
        direction = "DESC" if newest else "ASC"
        rows = await self._list(
            Message, f"SELECT {_columns(Message)} FROM messages WHERE {where} "
            f"ORDER BY created_at {direction}, seq {direction} LIMIT %s", parameters + (limit,),
        )
        return list(reversed(rows)) if newest else rows

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

    async def list_llm_steps(self, run_ids: list[str]) -> list[RunStep]:
        if not run_ids:
            return []
        return await self._list(
            RunStep, f"SELECT {_columns(RunStep)} FROM run_steps "
            "WHERE kind='llm_call' AND run_id=ANY(%s) ORDER BY run_id,seq", (run_ids,))

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
            if self._hub is not None:
                self._hub.detach(self._tenant_schema)
            else:
                await self._pool.close()


from .postgres_domains import (  # noqa: E402
    ApprovalMixin,
    AuditMixin,
    ConnectionMixin,
    MemoryMixin,
    NotificationMixin,
    QuestionMixin,
    ScheduleMixin,
    SettingsMixin,
    UsersMixin,
)


from .deliveries import PostgresDeliveries  # noqa: E402
from .tasks import TaskQueries  # noqa: E402
from .task_outcomes import TaskOutcomeStore  # noqa: E402
from .thread_metadata import PostgresThreadMetadata  # noqa: E402


class PostgresStore(
    TaskOutcomeStore, TaskQueries, PostgresThreadMetadata, PostgresDeliveries, ScheduleMixin, ApprovalMixin,
    QuestionMixin, AuditMixin,
    MemoryMixin, SettingsMixin,
    NotificationMixin, ConnectionMixin, UsersMixin, PostgresCore,
):
    """The full Postgres store: core plus every domain mixin."""
