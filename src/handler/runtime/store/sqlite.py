"""SQLite persistence, serialized on a single dedicated worker thread."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from .base import WorkSignal, WorkState
from .usage import SQLiteUsage
from .due_work import DueWriter
from .run_failures import (
    ALREADY_REPLAYED,
    FAILED_BEFORE_UPGRADE,
    FAILED_MARKER,
    NOT_LATEST,
    RunFailures,
    failure_row,
)
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
_JSON_FIELDS = {"payload", "blocks", "raw", "data", "usage", "trigger_event_ids",
                "context_links"}
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


# Columns added to existing tables after they shipped: SQLite has no
# ADD COLUMN IF NOT EXISTS, so each is added at connect when missing.
# (table, column, declaration). Postgres does the same in its schema files.
ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("memory_topics", "kind", "TEXT"),
    ("memory_topics", "links", "TEXT"),
    ("memory_topics", "source_untrusted", "INTEGER NOT NULL DEFAULT 0"),
    ("memory_topics", "updated_thread_key", "TEXT"),
    ("threads", "context_links", "TEXT"),
    ("threads", "context_hash", "TEXT"),
    ("runs", "cancel_requested_at", "TEXT"),
    ("runs", "cancel_requested_by", "TEXT"),
]
# Indexes on added columns, created once the columns exist.
ADDED_INDEXES = [
    "CREATE INDEX IF NOT EXISTS memory_topics_updated ON memory_topics(updated_at)",
    "CREATE INDEX IF NOT EXISTS memory_topics_kind ON memory_topics(kind, updated_at)",
]


def _add_columns(db: sqlite3.Connection) -> None:
    for table, column, declaration in ADDED_COLUMNS:
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    for statement in ADDED_INDEXES:
        db.execute(statement)


def _latest_on_thread(db: sqlite3.Connection, key: str, run_id: str, now: str) -> bool:
    """True when `run_id` is the thread's newest run and nothing else is queued or
    running there (see run_failures.NOT_LATEST)."""
    newest = db.execute("SELECT id FROM runs WHERE thread_key = ? "
                        "ORDER BY started_at DESC, rowid DESC LIMIT 1", (key,)).fetchone()
    if newest is None or newest["id"] != run_id:
        return False
    busy = db.execute(
        "SELECT 1 FROM threads WHERE key = ? AND lease_owner IS NOT NULL "
        "AND lease_until > ?", (key, now)).fetchone()
    pending = db.execute(
        "SELECT 1 FROM events WHERE thread_key = ? AND status = 'routed' "
        "AND consumed_by_run IS NULL LIMIT 1", (key,)).fetchone()
    return busy is None and pending is None


class SQLiteCore(DueWriter, SQLiteUsage):
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
        self._wake = WorkSignal()
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
                _add_columns(db)
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
                    db.execute(
                        "INSERT INTO threads "
                        "(key, type, lane, owner_user_id, harness, model, priority, "
                        "created_at, pending_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET "
                        "pending_since = COALESCE(threads.pending_since, excluded.pending_since)",
                        (thread.key, thread.type.value, thread.lane.value,
                         thread.owner_user_id, thread.harness, thread.model,
                         thread.priority, now, now),
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

    async def work_state(self) -> WorkState:
        def read(db: sqlite3.Connection) -> WorkState:
            now = _timestamp(self._clock())
            rows = db.execute(
                "SELECT lane, MAX(ready) AS ready, MIN(CASE WHEN ready = 0 THEN "
                "MAX(COALESCE(available_at, pending_since), COALESCE(lease_until, pending_since)) "
                "END) AS later FROM (SELECT lane, available_at, lease_until, pending_since, "
                "((available_at IS NULL OR available_at <= ?) "
                "AND (lease_until IS NULL OR lease_until < ?)) AS ready FROM threads "
                "WHERE pending_since IS NOT NULL AND dead = 0) GROUP BY lane",
                (now, now)).fetchall()
            lanes = frozenset(Lane(row["lane"]) for row in rows if row["ready"])
            later = [row["later"] for row in rows if row["later"] is not None]
            return WorkState(lanes, datetime.fromisoformat(min(later)) if later else None)

        return await self._call(read)

    async def has_claimable(self, lane: Lane) -> bool:
        def read(db: sqlite3.Connection) -> bool:
            now = _timestamp(self._clock())
            return db.execute(
                "SELECT EXISTS (SELECT 1 FROM threads WHERE lane = ? "
                "AND pending_since IS NOT NULL AND dead = 0 "
                "AND (available_at IS NULL OR available_at <= ?) "
                "AND (lease_until IS NULL OR lease_until < ?))",
                (lane.value, now, now)).fetchone()[0] == 1

        return await self._call(read)

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

    async def event_by_idempotency_key(self, key: str) -> Event | None:
        found = await self._list(Event, "SELECT * FROM events WHERE idempotency_key = ?", (key,))
        return found[0] if found else None

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
        self, lease: Lease, error: str, retry_in_seconds: float | None, *,
        run_id: str = "", event_ids: Sequence[str] | None = None,
    ) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            if retry_in_seconds is None:
                self._fail_terminal(db, lease.thread_key, error, run_id, event_ids)
                return True
            db.execute(
                "UPDATE threads SET attempts = attempts + 1, last_error = ?, "
                "available_at = ?, lease_owner = NULL, lease_until = NULL WHERE key = ?",
                (error, _timestamp(self._clock() + timedelta(seconds=retry_in_seconds)),
                 lease.thread_key),
            )
            return True

        failed = await self._fenced(lease, write)
        if failed:
            self._wake.set()
        return failed

    def _fail_terminal(self, db: sqlite3.Connection, key: str, error: str, run_id: str,
                       event_ids: Sequence[str] | None) -> None:
        """Accept the failure: consume its events, record it, leave the thread claimable."""
        thread = db.execute("SELECT type, attempts FROM threads WHERE key = ?", (key,)).fetchone()
        marker = run_id or FAILED_MARKER
        scope, params = "", ()
        if event_ids is not None:
            scope = f"AND id IN ({','.join('?' for _ in event_ids) or 'NULL'}) "
            params = tuple(event_ids)
        taken = {row["id"] for row in db.execute(
            "UPDATE events SET consumed_by_run = ? WHERE thread_key = ? AND status = 'routed' "
            f"AND consumed_by_run IS NULL {scope}RETURNING id", (marker, key, *params))}
        pending = db.execute(
            "SELECT 1 FROM events WHERE thread_key = ? AND status = 'routed' "
            "AND consumed_by_run IS NULL LIMIT 1", (key,)).fetchone()
        # attempts and backoff reset; last_error stays for debugging until a run succeeds.
        db.execute(
            "UPDATE threads SET pending_since = CASE WHEN ? THEN pending_since ELSE NULL END, "
            "attempts = 0, last_error = ?, dead = 0, available_at = NULL, "
            "lease_owner = NULL, lease_until = NULL WHERE key = ?", (bool(pending), error, key))
        if run_id:
            ordered = [i for i in (event_ids or []) if i in taken] + sorted(
                taken - set(event_ids or []))
            db.execute(
                "INSERT INTO run_failures (run_id, thread_key, thread_type, error, attempts, "
                "event_ids, consumed_by, failed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (run_id) DO NOTHING",
                (run_id, key, thread["type"], error[:2000], thread["attempts"] + 1,
                 json.dumps(ordered), marker, _timestamp(self._clock())))

    async def sweep_dead_threads(self) -> list[dict]:
        """Clear `dead` on threads an older release parked; idempotent.

        See PostgresCore.sweep_dead_threads.
        """
        def write(db: sqlite3.Connection) -> list[dict]:
            swept: list[dict] = []
            with _transaction(db):
                for thread in db.execute(
                        "SELECT key, type, attempts, last_error FROM threads WHERE dead "
                        "ORDER BY key").fetchall():
                    key = thread["key"]
                    event_ids = [r["id"] for r in db.execute(
                        "SELECT id FROM events WHERE thread_key = ? AND status = 'routed' "
                        "AND consumed_by_run IS NULL ORDER BY received_at, rowid", (key,))]
                    db.execute(
                        "UPDATE events SET consumed_by_run = ? WHERE thread_key = ? "
                        "AND status = 'routed' AND consumed_by_run IS NULL",
                        (FAILED_BEFORE_UPGRADE, key))
                    run = db.execute(
                        "SELECT id, finished_at FROM runs WHERE thread_key = ? "
                        "ORDER BY started_at DESC, rowid DESC LIMIT 1", (key,)).fetchone()
                    db.execute(
                        "UPDATE threads SET dead = 0, attempts = 0, available_at = NULL, "
                        "pending_since = NULL WHERE key = ?", (key,))
                    failed_at = (run and run["finished_at"]) or _timestamp(self._clock())
                    row = {"run_id": run["id"] if run else f"{FAILED_BEFORE_UPGRADE}:{key}",
                           "thread_key": key, "thread_type": thread["type"],
                           "error": thread["last_error"], "attempts": thread["attempts"],
                           "event_ids": event_ids, "consumed_by": FAILED_BEFORE_UPGRADE,
                           "failed_at": datetime.fromisoformat(failed_at)}
                    db.execute(
                        "INSERT INTO run_failures (run_id, thread_key, thread_type, error, "
                        "attempts, event_ids, consumed_by, failed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (run_id) DO NOTHING",
                        (row["run_id"], key, row["thread_type"], (row["error"] or "")[:2000],
                         row["attempts"], json.dumps(event_ids), FAILED_BEFORE_UPGRADE,
                         failed_at))
                    swept.append(row)
            return swept

        return await self._call(write)

    async def replay_failed_run(self, run_id: str, *, by: str | None = None,
                                latest_only: bool = False) -> dict | None:
        """Make a terminal failure's events pending again on its thread, once.

        See PostgresCore.replay_failed_run.
        """
        def write(db: sqlite3.Connection) -> dict | None:
            with _transaction(db):
                if latest_only:
                    existing = db.execute("SELECT * FROM run_failures WHERE run_id = ?",
                                          (run_id,)).fetchone()
                    if existing is None:
                        return None
                    if existing["replayed_at"] is None and not _latest_on_thread(
                            db, existing["thread_key"], run_id,
                            _timestamp(self._clock())):
                        return {**failure_row(existing), "replayed": False,
                                "requeued": 0, "reason": NOT_LATEST}
                row = db.execute(
                    "UPDATE run_failures SET replayed_at = ?, replayed_by = ? "
                    "WHERE run_id = ? AND replayed_at IS NULL RETURNING *",
                    (_timestamp(self._clock()), by, run_id)).fetchone()
                if row is None:
                    existing = db.execute("SELECT * FROM run_failures WHERE run_id = ?",
                                          (run_id,)).fetchone()
                    return None if existing is None else {
                        **failure_row(existing), "replayed": False, "requeued": 0,
                        "reason": ALREADY_REPLAYED}
                row = failure_row(row)
                ids = row["event_ids"]
                requeued = len(db.execute(
                    "UPDATE events SET consumed_by_run = NULL WHERE thread_key = ? "
                    f"AND id IN ({','.join('?' for _ in ids) or 'NULL'}) "
                    "AND consumed_by_run = ? RETURNING id",
                    (row["thread_key"], *ids, row["consumed_by"])).fetchall())
                if requeued:
                    db.execute(
                        "UPDATE threads SET pending_since = COALESCE(pending_since, ?), "
                        "dead = 0 WHERE key = ?", (_timestamp(self._clock()), row["thread_key"]))
                return {**row, "replayed": True, "requeued": requeued}

        result = await self._call(write)
        if result and result["requeued"]:
            self._wake.set()
        return result

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
        self, owner_user_id: str | None = None, limit: int = 100,
        types: Sequence[str] | None = None, exclude_types: Sequence[str] | None = None,
    ) -> list[Thread]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if owner_user_id is not None:
            clauses.append("(owner_user_id = ? OR type <> 'chat')")
            parameters.append(owner_user_id)
        if types:
            clauses.append(f"type IN ({', '.join('?' * len(types))})")
            parameters.extend(types)
        if exclude_types:
            clauses.append(f"type NOT IN ({', '.join('?' * len(exclude_types))})")
            parameters.extend(exclude_types)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        parameters.append(limit)
        return await self._list(
            Thread, f"SELECT * FROM threads {where}ORDER BY COALESCE((SELECT MAX(created_at) FROM messages WHERE thread_key=threads.key), threads.created_at) DESC, rowid DESC LIMIT ?",
            tuple(parameters),
        )

    async def set_session(self, lease: Lease, session_id: str) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            # A new session holds no Handler context block yet.
            db.execute("UPDATE threads SET session_id = ?, context_hash = NULL WHERE key = ?",
                       (session_id, lease.thread_key))
            return True

        return await self._fenced(lease, write)

    async def set_context_hash(self, lease: Lease, context_hash: str | None) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            db.execute("UPDATE threads SET context_hash = ? WHERE key = ?",
                       (context_hash, lease.thread_key))
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

    async def request_run_cancel(self, run_id: str, by: str | None) -> bool:
        """Set the run's cancel flag; False unless it is still running."""
        return await self._call(lambda db: bool(db.execute(
            "UPDATE runs SET cancel_requested_at = COALESCE(cancel_requested_at, ?), "
            "cancel_requested_by = COALESCE(cancel_requested_by, ?) "
            "WHERE id = ? AND status = ?",
            (_timestamp(self._clock()), by, run_id, RunStatus.RUNNING.value)).rowcount))

    async def run_cancel_requested(self, run_id: str) -> bool:
        def read(db: sqlite3.Connection) -> bool:
            row = db.execute("SELECT cancel_requested_at FROM runs WHERE id = ?",
                             (run_id,)).fetchone()
            return row is not None and row[0] is not None

        return await self._call(read)

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

    async def list_llm_steps(self, run_ids: list[str]) -> list[RunStep]:
        rows = []
        for start in range(0, len(run_ids), 500):
            batch = run_ids[start:start + 500]
            rows.extend(await self._list(
                RunStep, f"SELECT * FROM run_steps WHERE kind='llm_call' AND run_id IN "
                f"({','.join('?' for _ in batch)}) ORDER BY run_id,seq", tuple(batch)))
        return rows

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
from .sqlite_gateway import ApprovalMixin, AuditMixin, QuestionMixin  # noqa: E402
from .sqlite_memory import MemoryMixin, NotificationMixin, SettingsMixin  # noqa: E402
from .sqlite_schedules import ScheduleMixin  # noqa: E402
from .sqlite_users import UsersMixin  # noqa: E402


from .deliveries import SQLiteDeliveries  # noqa: E402
from .tasks import TaskQueries  # noqa: E402
from .task_outcomes import TaskOutcomeStore  # noqa: E402
from .thread_imports import SQLiteThreadImports  # noqa: E402
from .thread_metadata import SQLiteThreadMetadata  # noqa: E402
from .due_work import SQLiteDueWork  # noqa: E402


class SQLiteStore(
    TaskOutcomeStore, TaskQueries, RunFailures, SQLiteThreadImports, SQLiteThreadMetadata, SQLiteDeliveries, ScheduleMixin, ApprovalMixin,
    QuestionMixin, AuditMixin,
    MemoryMixin, SettingsMixin,
    NotificationMixin, ConnectionMixin, UsersMixin, SQLiteDueWork, SQLiteCore,
):
    """The full SQLite store: core plus every domain mixin."""
