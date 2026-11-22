"""One connection pool and one LISTEN connection shared by every tenant in the process.

A PostgresCore given a hub runs each transaction on the shared pool, opening with
`SET LOCAL search_path` for its own schema. A pool connection's default search_path
is the control schema, which holds no tenant tables, so SQL that was never scoped
fails instead of reaching another tenant's rows.

Work notifications arrive on one channel whose payload is the tenant's schema.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

CONTROL_SCHEMA = "handler_control"
SHARED_CHANNEL = "handler_work_shared"
SCHEMA_LOCK = 7_283_001
DEFAULT_POOL_MAX = 11  # with the LISTEN connection, 12 for the whole process
DUE_CHANNEL = "handler_due_work"  # due work changed (store/due_work.py); payload: schema

# The host timer's rows for every tenant in the database (store/due_work.py).
# `tenant` is the tenant's schema name. Additive: older releases ignore it.
DUE_WORK_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {CONTROL_SCHEMA};
CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.due_work (
    tenant TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    version BIGINT NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, kind, ref)
);
CREATE INDEX IF NOT EXISTS due_work_due ON {CONTROL_SCHEMA}.due_work(due_at);
"""

_CONTROL_DDL = DUE_WORK_DDL + f"""
CREATE SCHEMA IF NOT EXISTS {CONTROL_SCHEMA};
CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.tenant_schema_versions (
    schema_name TEXT NOT NULL,
    component TEXT NOT NULL,
    version TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (schema_name, component)
);
"""


class PostgresHub:
    """The process-wide connections for one database; the host owns its lifetime."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = DEFAULT_POOL_MAX) -> None:
        self._dsn = dsn
        self._options = f"-c timezone=UTC -c search_path={CONTROL_SCHEMA}"
        # Auto-prepared statements stay on the connection across tenants. Postgres
        # re-resolves them per search_path, but rejects a changed result shape:
        # `SELECT *` would fail wherever two tenants' columns differ in order.
        self.pool = AsyncConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=False,
            kwargs={"row_factory": dict_row, "options": self._options,
                    "prepare_threshold": None},
        )
        self.ddl_lock = asyncio.Lock()  # DDL waits here, not holding a pool connection
        self._wakes: dict[str, asyncio.Event] = {}
        self._ready: asyncio.Task[None] | None = None
        self._listener: asyncio.Task[None] | None = None
        self._listening = asyncio.Event()
        self._closed = False
        self.on_unattached: Callable[[str], None] | None = None  # work for an inactive schema
        self.on_listening: Callable[[], None] | None = None  # notices may have been missed
        self.on_due: Callable[[], None] | None = None  # due work changed (the host timer)
        self._due_queue = None

    async def _setup(self) -> None:
        await self.pool.open()
        async with self.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
            await conn.execute(_CONTROL_DDL)
        self._listener = asyncio.create_task(self._listen())
        await self._listening.wait()

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("PostgresHub is closed")
        if self._ready is None:
            self._ready = asyncio.create_task(self._setup())
        await asyncio.shield(self._ready)

    def attach(self, schema: str, wake: asyncio.Event) -> None:
        """Route `schema`'s notifications to `wake`."""
        if schema in self._wakes:
            raise ValueError(f"schema {schema!r} is already attached to this hub")
        self._wakes[schema] = wake

    def due_queue(self):
        """The one due-work queue of this database (store/due_work.py)."""
        if self._due_queue is None:
            from .due_work import PostgresDueQueue

            self._due_queue = PostgresDueQueue(self.pool.connection, ensure=self.start)
        return self._due_queue

    def detach(self, schema: str) -> None:
        self._wakes.pop(schema, None)

    async def _listen(self) -> None:
        while not self._closed:
            try:
                conn = await AsyncConnection.connect(
                    self._dsn, autocommit=True, options=self._options)
                async with conn:
                    await conn.execute(
                        sql.SQL("LISTEN {}").format(sql.Identifier(SHARED_CHANNEL)))
                    await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(DUE_CHANNEL)))
                    self._listening.set()
                    for wake in self._wakes.values():  # anything missed while offline
                        wake.set()
                    if self.on_listening is not None:
                        self.on_listening()
                    if self.on_due is not None:
                        self.on_due()
                    async for notice in conn.notifies():
                        if notice.channel == DUE_CHANNEL:
                            if self.on_due is not None:
                                self.on_due()
                            continue
                        wake = self._wakes.get(notice.payload)
                        if wake is not None:
                            wake.set()
                        elif self.on_unattached is not None:
                            self.on_unattached(notice.payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._listening.set()
                await asyncio.sleep(1)

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
            await self.pool.close()
