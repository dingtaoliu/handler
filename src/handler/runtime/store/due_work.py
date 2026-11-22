"""Due work: one row per piece of time-driven work, read by the host's one timer.

A row is (tenant, kind, ref, due_at). The work's own transaction writes it: creating
or advancing a schedule, creating an approval or question with an expiry, connecting
a mailbox, holding an input over a plan's limit. The host timer (`runtime/timer.py`)
claims due rows, runs the handler registered for the row's kind, and the handler
returns the next due time (or None: the row is removed).

Where the rows live:

- Postgres: `handler_control.due_work`, one table for every tenant in the database.
  `tenant` is the tenant's schema name (unique in the database, and what every store
  knows), so claiming due work across tenants reads the control schema only and an
  idle tenant costs no tenant-scoped query.
- SQLite (the personal instance and tests): a `due_work` table in the store's own
  file (schema_due_work.sql), tenant `local`. The same timer reads it.

`version` is bumped by every write from the work's own transaction. When the timer
finishes a row whose version changed while its handler ran (for example the
schedule.fire handler advanced the schedule, or a person edited it), the stored
due time wins over the handler's result. Leases fence the timer's own writes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from ..tenancy.limits import next_reset
from .postgres_hub import CONTROL_SCHEMA, DUE_CHANNEL, DUE_WORK_DDL

DUE_TABLE = f"{CONTROL_SCHEMA}.due_work"
LOCAL_TABLE = "due_work"
# A Postgres store with its own connections (a lone instance, no hub) keeps its rows
# in its own schema instead, and never touches handler_control.
LOCAL_PG_DDL = "\n".join(line for line in DUE_WORK_DDL.replace(DUE_TABLE, LOCAL_TABLE)
                         .splitlines() if not line.startswith("CREATE SCHEMA"))
LOCAL_TENANT = "local"  # the tenant key of a SQLite store

# The core kinds (handlers in timer.py / server.py; integrations add their own).
SCHEDULE_FIRE = "schedule.fire"
APPROVAL_EXPIRE = "approval.expire"
QUESTION_EXPIRE = "question.expire"
INPUTS_RELEASE = "inputs.release"
RETENTION_CLEANUP = "retention.cleanup"
TENANT_REF = "-"  # the ref of a kind with one row per tenant
# A tenant's first retention cleanup, then daily (retention.py): not at the start
# itself, so a starting tenant has nothing due at once.
RETENTION_FIRST_DELAY = timedelta(hours=1)


@dataclass(frozen=True)
class DueItem:
    tenant: str
    kind: str
    ref: str
    due_at: datetime
    version: int
    attempts: int


def schedule_due(schedule: Any) -> datetime | None:
    """When the host timer must fire this schedule next (None: never)."""
    return schedule.next_run_at if schedule.enabled else None


def _ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _dt(value: Any) -> datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


# --- writes from the work's own transaction -----------------------------------

def pg_upsert_sql(keep_existing: bool, table: str = DUE_TABLE) -> str:
    conflict = ("DO NOTHING" if keep_existing else
                f"DO UPDATE SET due_at = excluded.due_at, version = {table}.version + 1, "
                "attempts = 0, last_error = NULL, updated_at = excluded.updated_at")
    return (f"INSERT INTO {table} (tenant, kind, ref, due_at, created_at, updated_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant, kind, ref) {conflict}")


def pg_delete_sql(table: str = DUE_TABLE) -> str:
    return f"DELETE FROM {table} WHERE tenant = %s AND kind = %s AND ref = %s"


PG_NOTIFY_SQL = "SELECT pg_notify(%s, %s)"


async def pg_set_due(conn: Any, tenant: str, kind: str, ref: str, due_at: datetime | None,
                     now: datetime, *, keep_existing: bool = False,
                     table: str = DUE_TABLE) -> None:
    """In the caller's transaction: schedule (due_at) or remove (None) one row."""
    if due_at is None:
        await conn.execute(pg_delete_sql(table), (tenant, kind, ref))
    else:
        await conn.execute(pg_upsert_sql(keep_existing, table),
                           (tenant, kind, ref, due_at, now, now))
    await conn.execute(PG_NOTIFY_SQL, (DUE_CHANNEL, tenant))


def sqlite_upsert_sql(keep_existing: bool) -> str:
    conflict = ("DO NOTHING" if keep_existing else
                "DO UPDATE SET due_at = excluded.due_at, version = due_work.version + 1, "
                "attempts = 0, last_error = NULL, updated_at = excluded.updated_at")
    return ("INSERT INTO due_work (tenant, kind, ref, due_at, created_at, updated_at) "
            f"VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (tenant, kind, ref) {conflict}")


SQLITE_DELETE_SQL = "DELETE FROM due_work WHERE tenant = ? AND kind = ? AND ref = ?"


def sqlite_set_due(db: sqlite3.Connection, kind: str, ref: str, due_at: datetime | None,
                   now: datetime, *, keep_existing: bool = False) -> None:
    if due_at is None:
        db.execute(SQLITE_DELETE_SQL, (LOCAL_TENANT, kind, ref))
    else:
        db.execute(sqlite_upsert_sql(keep_existing),
                   (LOCAL_TENANT, kind, ref, _ts(due_at), _ts(now), _ts(now)))


class DueWriter:
    """What a store offers for due rows (mixed into SQLiteStore and PostgresStore).

    `on_due_changed` is the in-process hint (the host timer's wake); stores call it
    after a commit that wrote due work. Postgres also sends `NOTIFY handler_due_work`
    in the transaction for other processes.
    """

    on_due_changed: Callable[[], None] | None = None

    def _due_written(self) -> None:
        if self.on_due_changed is not None:
            self.on_due_changed()


# --- the timer's side ----------------------------------------------------------

class DueQueue(Protocol):
    """Claims and settles due rows for a set of tenants (see HostTimer)."""

    async def claim(self, owner: str, tenants: Sequence[str], now: datetime, limit: int,
                    lease_seconds: float) -> list[DueItem]: ...

    async def renew(self, owner: str, until: datetime) -> None: ...

    async def finish(self, item: DueItem, owner: str, next_due: datetime | None,
                     now: datetime) -> None: ...

    async def fail(self, item: DueItem, owner: str, error: str, retry_at: datetime,
                   now: datetime) -> None: ...

    async def postpone(self, item: DueItem, owner: str, until: datetime) -> None: ...

    async def release(self, owner: str) -> None: ...

    async def next_due(self, tenants: Sequence[str], now: datetime) -> datetime | None: ...


class PostgresDueQueue:
    """`handler_control.due_work` through the hub's pool (every statement names the
    control table in full, so it never sets a tenant's search_path), or a lone
    store's own `due_work` table through its own connections."""

    def __init__(self, connect: Callable[[], Any], ensure: Callable[[], Any] | None = None,
                 table: str = DUE_TABLE) -> None:
        self._connect = connect
        self._ensure = ensure
        self._table = table

    async def _conn(self):
        if self._ensure is not None:
            await self._ensure()
        return self._connect()

    async def claim(self, owner, tenants, now, limit, lease_seconds):
        if limit <= 0 or not tenants:
            return []
        async with await self._conn() as conn:
            cursor = await conn.execute(
                f"WITH due AS (SELECT tenant, kind, ref FROM {self._table} "
                "WHERE due_at <= %(now)s AND tenant = ANY(%(tenants)s) "
                "AND (lease_until IS NULL OR lease_until < %(now)s) "
                "ORDER BY due_at LIMIT %(limit)s FOR UPDATE SKIP LOCKED) "
                f"UPDATE {self._table} d SET lease_owner = %(owner)s, lease_until = %(until)s "
                "FROM due WHERE d.tenant = due.tenant AND d.kind = due.kind AND d.ref = due.ref "
                "RETURNING d.tenant, d.kind, d.ref, d.due_at, d.version, d.attempts",
                {"now": now, "tenants": list(tenants), "limit": limit, "owner": owner,
                 "until": now + timedelta(seconds=lease_seconds)})
            rows = await cursor.fetchall()
        return sorted((DueItem(r["tenant"], r["kind"], r["ref"], r["due_at"], r["version"],
                               r["attempts"]) for r in rows), key=lambda i: i.due_at)

    async def renew(self, owner, until):
        async with await self._conn() as conn:
            await conn.execute(f"UPDATE {self._table} SET lease_until = %s WHERE lease_owner = %s",
                               (until, owner))

    async def finish(self, item, owner, next_due, now):
        key = (item.tenant, item.kind, item.ref)
        async with await self._conn() as conn:
            if next_due is None:
                cursor = await conn.execute(
                    f"DELETE FROM {self._table} WHERE tenant = %s AND kind = %s AND ref = %s "
                    "AND lease_owner = %s AND version = %s", (*key, owner, item.version))
                if cursor.rowcount:
                    return
            await conn.execute(
                f"UPDATE {self._table} SET lease_owner = NULL, lease_until = NULL, "
                "due_at = CASE WHEN version = %(v)s AND %(next)s::timestamptz IS NOT NULL "
                "THEN %(next)s ELSE due_at END, "
                "attempts = CASE WHEN version = %(v)s THEN 0 ELSE attempts END, "
                "last_error = CASE WHEN version = %(v)s THEN NULL ELSE last_error END, "
                "updated_at = %(now)s "
                "WHERE tenant = %(t)s AND kind = %(k)s AND ref = %(r)s AND lease_owner = %(o)s",
                {"v": item.version, "next": next_due, "now": now, "t": item.tenant,
                 "k": item.kind, "r": item.ref, "o": owner})

    async def fail(self, item, owner, error, retry_at, now):
        async with await self._conn() as conn:
            await conn.execute(
                f"UPDATE {self._table} SET lease_owner = NULL, lease_until = NULL, "
                "attempts = attempts + 1, last_error = %(e)s, updated_at = %(now)s, "
                "due_at = CASE WHEN version = %(v)s THEN %(retry)s ELSE due_at END "
                "WHERE tenant = %(t)s AND kind = %(k)s AND ref = %(r)s AND lease_owner = %(o)s",
                {"e": error[:2000], "now": now, "v": item.version, "retry": retry_at,
                 "t": item.tenant, "k": item.kind, "r": item.ref, "o": owner})

    async def postpone(self, item, owner, until):
        async with await self._conn() as conn:
            await conn.execute(
                f"UPDATE {self._table} SET lease_owner = NULL, lease_until = NULL, "
                "due_at = CASE WHEN version = %s THEN %s ELSE due_at END "
                "WHERE tenant = %s AND kind = %s AND ref = %s AND lease_owner = %s",
                (item.version, until, item.tenant, item.kind, item.ref, owner))

    async def release(self, owner):
        async with await self._conn() as conn:
            await conn.execute(f"UPDATE {self._table} SET lease_owner = NULL, lease_until = NULL "
                               "WHERE lease_owner = %s", (owner,))

    async def next_due(self, tenants, now):
        if not tenants:
            return None
        async with await self._conn() as conn:
            cursor = await conn.execute(
                "SELECT MIN(CASE WHEN lease_until IS NOT NULL AND lease_until >= %s "
                f"THEN lease_until ELSE due_at END) AS due FROM {self._table} "
                "WHERE tenant = ANY(%s)", (now, list(tenants)))
            return (await cursor.fetchone())["due"]


class SQLiteDueQueue:
    """The `due_work` table in one SQLite store's file (tenant `local`)."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def claim(self, owner, tenants, now, limit, lease_seconds):
        if limit <= 0 or LOCAL_TENANT not in tenants:
            return []
        until = _ts(now + timedelta(seconds=lease_seconds))

        def run(db: sqlite3.Connection) -> list[DueItem]:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    "SELECT tenant, kind, ref, due_at, version, attempts FROM due_work "
                    "WHERE tenant = ? AND due_at <= ? AND (lease_until IS NULL OR lease_until < ?) "
                    "ORDER BY due_at LIMIT ?", (LOCAL_TENANT, _ts(now), _ts(now), limit)
                ).fetchall()
                for row in rows:
                    db.execute("UPDATE due_work SET lease_owner = ?, lease_until = ? "
                               "WHERE tenant = ? AND kind = ? AND ref = ?",
                               (owner, until, row["tenant"], row["kind"], row["ref"]))
                db.commit()
            except BaseException:
                db.rollback()
                raise
            return [DueItem(r["tenant"], r["kind"], r["ref"], _dt(r["due_at"]), r["version"],
                            r["attempts"]) for r in rows]

        return await self._store._call(run)

    async def renew(self, owner, until):
        await self._store._call(lambda db: db.execute(
            "UPDATE due_work SET lease_until = ? WHERE lease_owner = ?", (_ts(until), owner)))

    async def finish(self, item, owner, next_due, now):
        key = (item.tenant, item.kind, item.ref)

        def run(db: sqlite3.Connection) -> None:
            if next_due is None and db.execute(
                    "DELETE FROM due_work WHERE tenant = ? AND kind = ? AND ref = ? "
                    "AND lease_owner = ? AND version = ?", (*key, owner, item.version)).rowcount:
                return
            same = "version = :v"
            db.execute(
                "UPDATE due_work SET lease_owner = NULL, lease_until = NULL, "
                f"due_at = CASE WHEN {same} AND :next IS NOT NULL THEN :next ELSE due_at END, "
                f"attempts = CASE WHEN {same} THEN 0 ELSE attempts END, "
                f"last_error = CASE WHEN {same} THEN NULL ELSE last_error END, "
                "updated_at = :now WHERE tenant = :t AND kind = :k AND ref = :r "
                "AND lease_owner = :o",
                {"v": item.version, "next": _ts(next_due) if next_due else None, "now": _ts(now),
                 "t": item.tenant, "k": item.kind, "r": item.ref, "o": owner})

        await self._store._call(run)

    async def fail(self, item, owner, error, retry_at, now):
        await self._store._call(lambda db: db.execute(
            "UPDATE due_work SET lease_owner = NULL, lease_until = NULL, "
            "attempts = attempts + 1, last_error = ?, updated_at = ?, "
            "due_at = CASE WHEN version = ? THEN ? ELSE due_at END "
            "WHERE tenant = ? AND kind = ? AND ref = ? AND lease_owner = ?",
            (error[:2000], _ts(now), item.version, _ts(retry_at), item.tenant, item.kind,
             item.ref, owner)))

    async def postpone(self, item, owner, until):
        await self._store._call(lambda db: db.execute(
            "UPDATE due_work SET lease_owner = NULL, lease_until = NULL, "
            "due_at = CASE WHEN version = ? THEN ? ELSE due_at END "
            "WHERE tenant = ? AND kind = ? AND ref = ? AND lease_owner = ?",
            (item.version, _ts(until), item.tenant, item.kind, item.ref, owner)))

    async def release(self, owner):
        await self._store._call(lambda db: db.execute(
            "UPDATE due_work SET lease_owner = NULL, lease_until = NULL WHERE lease_owner = ?",
            (owner,)))

    async def next_due(self, tenants, now):
        if LOCAL_TENANT not in tenants:
            return None
        value = await self._store._call(lambda db: db.execute(
            "SELECT MIN(CASE WHEN lease_until IS NOT NULL AND lease_until >= ? "
            "THEN lease_until ELSE due_at END) FROM due_work WHERE tenant = ?",
            (_ts(now), LOCAL_TENANT)).fetchone()[0])
        return _dt(value) if value else None



# --- store mixins ----------------------------------------------------------------

class SQLiteDueWork(DueWriter):
    """Due work on SQLiteCore (self._call, self._clock)."""

    @property
    def due_tenant(self) -> str:
        return LOCAL_TENANT

    def due_queue(self) -> SQLiteDueQueue:
        queue = getattr(self, "_due_queue", None)
        if queue is None:
            queue = self._due_queue = SQLiteDueQueue(self)
        return queue

    async def set_due(self, kind: str, ref: str, due_at: datetime | None, *,
                      keep_existing: bool = False) -> None:
        """Schedule (or with None remove) one row in its own transaction."""
        now = self._clock()
        await self._call(lambda db: sqlite_set_due(db, kind, ref, due_at, now,
                                                   keep_existing=keep_existing))
        self._due_written()

    def due_statements(self, kind: str, ref: str, due_at: datetime | None, *,
                       keep_existing: bool = False) -> list[tuple[str, tuple]]:
        """The same write as (sql, params) with `?` placeholders, for a caller's
        own transaction (e.g. MailStore._script). Call `_due_written` after commit."""
        if due_at is None:
            return [(SQLITE_DELETE_SQL, (LOCAL_TENANT, kind, ref))]
        now = _ts(self._clock())
        return [(sqlite_upsert_sql(keep_existing),
                 (LOCAL_TENANT, kind, ref, _ts(due_at), now, now))]

    async def prune_due(self, kind: str, keep_refs: Sequence[str]) -> None:
        """Remove this kind's rows whose ref is not in `keep_refs`."""
        keep = list(keep_refs)
        marks = ", ".join("?" for _ in keep) or "NULL"
        await self._call(lambda db: db.execute(
            f"DELETE FROM due_work WHERE tenant = ? AND kind = ? AND ref NOT IN ({marks})",
            (LOCAL_TENANT, kind, *keep)))

    async def due_rows(self) -> list[dict[str, Any]]:
        """This tenant's rows (tests and diagnostics)."""
        return await self._call(lambda db: [dict(r) for r in db.execute(
            "SELECT * FROM due_work WHERE tenant = ? ORDER BY due_at, kind, ref",
            (LOCAL_TENANT,))])

    async def backfill_due_work(self, *, schedules: bool = True, held: bool = False) -> None:
        """Make due_work match the rows it describes; idempotent, one transaction.

        Covers rows written before due_work existed (or by an older release during a
        rollback): enabled schedules (a missed next_run_at stays in the past, so it
        fires once and the schedule then moves to its next slot from now), pending
        approvals and questions with an expiry, and the daily retention row. Rows
        whose work is gone are removed.
        """
        now = _ts(self._clock())
        t = LOCAL_TENANT

        def run(db: sqlite3.Connection) -> None:
            db.execute("BEGIN IMMEDIATE")
            try:
                if schedules:
                    db.execute(
                        "INSERT INTO due_work (tenant, kind, ref, due_at, created_at, updated_at) "
                        "SELECT ?, ?, id, next_run_at, ?, ? FROM schedules "
                        "WHERE enabled = 1 AND next_run_at IS NOT NULL AND true "
                        "ON CONFLICT (tenant, kind, ref) DO UPDATE SET due_at = excluded.due_at, "
                        "version = due_work.version + 1, updated_at = excluded.updated_at "
                        "WHERE due_work.due_at <> excluded.due_at AND due_work.lease_owner IS NULL",
                        (t, SCHEDULE_FIRE, now, now))
                    db.execute(
                        "DELETE FROM due_work WHERE tenant = ? AND kind = ? AND ref NOT IN "
                        "(SELECT id FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL)",
                        (t, SCHEDULE_FIRE))
                else:  # a plan without automations: the plan change backfills again
                    db.execute("DELETE FROM due_work WHERE tenant = ? AND kind = ?",
                               (t, SCHEDULE_FIRE))
                for kind, table in ((APPROVAL_EXPIRE, "approvals"),
                                    (QUESTION_EXPIRE, "questions")):
                    db.execute(
                        "INSERT INTO due_work (tenant, kind, ref, due_at, created_at, updated_at) "
                        f"SELECT ?, ?, id, expires_at, ?, ? FROM {table} "
                        "WHERE status = 'pending' AND expires_at IS NOT NULL AND true "
                        "ON CONFLICT (tenant, kind, ref) DO NOTHING", (t, kind, now, now))
                    db.execute(
                        "DELETE FROM due_work WHERE tenant = ? AND kind = ? AND ref NOT IN "
                        f"(SELECT id FROM {table} WHERE status = 'pending' "
                        "AND expires_at IS NOT NULL)", (t, kind))
                db.execute(sqlite_upsert_sql(True), (
                    t, RETENTION_CLEANUP, TENANT_REF,
                    _ts(self._clock() + RETENTION_FIRST_DELAY), now, now))
                db.commit()
            except BaseException:
                db.rollback()
                raise

        await self._call(run)
        self._due_written()


class PostgresDueWork(DueWriter):
    """Due work on PostgresCore: rows in handler_control.due_work under this schema."""

    @property
    def due_tenant(self) -> str:
        return self._tenant_schema

    def due_queue(self) -> PostgresDueQueue:
        if self._hub is not None:
            return self._hub.due_queue()
        queue = getattr(self, "_due_queue", None)
        if queue is None:
            queue = self._due_queue = PostgresDueQueue(self._conn, table=LOCAL_TABLE)
        return queue

    async def set_due(self, kind: str, ref: str, due_at: datetime | None, *,
                      keep_existing: bool = False) -> None:
        async with self._conn() as conn:
            await pg_set_due(conn, self._tenant_schema, kind, ref, due_at, self._clock(),
                             keep_existing=keep_existing, table=self._due_table)
        self._due_written()

    def due_statements(self, kind: str, ref: str, due_at: datetime | None, *,
                       keep_existing: bool = False) -> list[tuple[str, tuple]]:
        """As SQLiteDueWork.due_statements (`?` placeholders, for MailStore._script)."""
        tenant, now = self._tenant_schema, self._clock()
        if due_at is None:
            write = (pg_delete_sql(self._due_table).replace("%s", "?"), (tenant, kind, ref))
        else:
            write = (pg_upsert_sql(keep_existing, self._due_table).replace("%s", "?"),
                     (tenant, kind, ref, due_at, now, now))
        return [write, (PG_NOTIFY_SQL.replace("%s", "?"), (DUE_CHANNEL, tenant))]

    async def prune_due(self, kind: str, keep_refs: Sequence[str]) -> None:
        table = self._due_table
        async with self._conn() as conn:
            await conn.execute(f"DELETE FROM {table} WHERE tenant = %s AND kind = %s "
                               "AND NOT (ref = ANY(%s))",
                               (self._tenant_schema, kind, list(keep_refs)))

    async def due_rows(self) -> list[dict[str, Any]]:
        table = self._due_table
        async with self._conn() as conn:
            cursor = await conn.execute(f"SELECT * FROM {table} WHERE tenant = %s "
                                        "ORDER BY due_at, kind, ref", (self._tenant_schema,))
            return [dict(row) for row in await cursor.fetchall()]

    async def backfill_due_work(self, *, schedules: bool = True, held: bool = False) -> None:
        """As SQLiteDueWork.backfill_due_work, plus held inputs (registry tenants)."""
        t, now, table = self._tenant_schema, self._clock(), self._due_table
        async with self._conn() as conn:
            if schedules:
                await conn.execute(
                    f"INSERT INTO {table} (tenant, kind, ref, due_at, created_at, updated_at) "
                    "SELECT %s, %s, id, next_run_at, %s, %s FROM schedules "
                    "WHERE enabled AND next_run_at IS NOT NULL "
                    "ON CONFLICT (tenant, kind, ref) DO UPDATE SET due_at = excluded.due_at, "
                    f"version = {table}.version + 1, updated_at = excluded.updated_at "
                    f"WHERE {table}.due_at <> excluded.due_at "
                    f"AND {table}.lease_owner IS NULL", (t, SCHEDULE_FIRE, now, now))
                await conn.execute(
                    f"DELETE FROM {table} WHERE tenant = %s AND kind = %s AND ref NOT IN "
                    "(SELECT id FROM schedules WHERE enabled AND next_run_at IS NOT NULL)",
                    (t, SCHEDULE_FIRE))
            else:
                await conn.execute(f"DELETE FROM {table} WHERE tenant = %s AND kind = %s",
                                   (t, SCHEDULE_FIRE))
            for kind, source in ((APPROVAL_EXPIRE, "approvals"), (QUESTION_EXPIRE, "questions")):
                await conn.execute(
                    f"INSERT INTO {table} (tenant, kind, ref, due_at, created_at, updated_at) "
                    f"SELECT %s, %s, id, expires_at, %s, %s FROM {source} "
                    "WHERE status = 'pending' AND expires_at IS NOT NULL "
                    "ON CONFLICT (tenant, kind, ref) DO NOTHING", (t, kind, now, now))
                await conn.execute(
                    f"DELETE FROM {table} WHERE tenant = %s AND kind = %s AND ref NOT IN "
                    f"(SELECT id FROM {source} WHERE status = 'pending' "
                    "AND expires_at IS NOT NULL)", (t, kind))
            if held:
                cursor = await conn.execute("SELECT EXISTS (SELECT 1 FROM held_inputs) AS any")
                if (await cursor.fetchone())["any"]:
                    # The caller released what this month allows; the rest waits
                    # for the month boundary.
                    await conn.execute(pg_upsert_sql(True, table),
                                       (t, INPUTS_RELEASE, TENANT_REF, next_reset(now), now, now))
            await conn.execute(pg_upsert_sql(True, table),
                               (t, RETENTION_CLEANUP, TENANT_REF,
                                now + RETENTION_FIRST_DELAY, now, now))
            await conn.execute(PG_NOTIFY_SQL, (DUE_CHANNEL, t))
        self._due_written()
