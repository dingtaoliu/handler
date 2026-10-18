"""Schedule persistence composed with SQLiteCore."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from .tasks import announce_routed
from ..types import Event, EventStatus, Schedule, ThreadSpec
from .sqlite import _insert, _record, _timestamp, _transaction, _values


def _schedule_values(schedule: Schedule) -> dict[str, Any]:
    values = _values(schedule)
    values["notify"] = json.dumps(schedule.notify)
    return values


def _schedule(row: sqlite3.Row) -> Schedule:
    schedule = _record(Schedule, row)
    schedule.notify = json.loads(row["notify"])
    schedule.enabled = bool(row["enabled"])
    schedule.one_shot = bool(row["one_shot"])
    for name in ("next_run_at", "last_run_at"):
        value = row[name]
        setattr(schedule, name, datetime.fromisoformat(value) if value else None)
    return schedule


class ScheduleMixin:
    async def create_schedule(self, schedule: Schedule) -> None:
        await self._call(lambda db: _insert(db, "schedules", _schedule_values(schedule)))

    async def get_schedule(self, schedule_id: str) -> Schedule | None:
        def read(db: sqlite3.Connection) -> Schedule | None:
            row = db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
            return _schedule(row) if row else None

        return await self._call(read)

    async def list_schedules(self, include_disabled: bool = False) -> list[Schedule]:
        where = "" if include_disabled else "WHERE enabled = 1 "
        return await self._call(lambda db: [
            _schedule(row) for row in db.execute(
                f"SELECT * FROM schedules {where}ORDER BY created_at, id"
            )
        ])

    async def update_schedule(self, schedule: Schedule) -> None:
        values = _schedule_values(schedule)
        assignments = ", ".join(f"{name} = :{name}" for name in values if name != "id")
        await self._call(lambda db: db.execute(
            f"UPDATE schedules SET {assignments} WHERE id = :id", values,
        ))

    async def delete_schedule(self, schedule_id: str) -> bool:
        return await self._call(lambda db: bool(db.execute(
            "DELETE FROM schedules WHERE id = ?", (schedule_id,),
        ).rowcount))

    async def next_due_at(self) -> datetime | None:
        def read(db: sqlite3.Connection) -> datetime | None:
            value = db.execute(
                "SELECT MIN(next_run_at) FROM schedules WHERE enabled = 1"
            ).fetchone()[0]
            return datetime.fromisoformat(value) if value else None

        return await self._call(read)

    async def due_schedules(self, now: datetime, limit: int = 50) -> list[Schedule]:
        return await self._call(lambda db: [
            _schedule(row) for row in db.execute(
                "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ? "
                "ORDER BY next_run_at, id LIMIT ?", (_timestamp(now), max(0, limit)),
            )
        ])

    async def fire_schedule(
        self,
        schedule: Schedule,
        expected_next_run_at: datetime,
        next_run_at: datetime | None,
        event: Event,
        thread: ThreadSpec,
    ) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            with _transaction(db):
                advanced = db.execute(
                    "UPDATE schedules SET last_run_at = ?, next_run_at = ?, "
                    "enabled = CASE WHEN ? THEN 0 ELSE enabled END "
                    "WHERE id = ? AND enabled = 1 AND next_run_at = ?",
                    (_timestamp(expected_next_run_at),
                     _timestamp(next_run_at) if next_run_at is not None else None,
                     next_run_at is None, schedule.id, _timestamp(expected_next_run_at)),
                ).rowcount
                if not advanced:
                    return False

                # SQLiteCore.put's insert and routing logic, in this transaction
                # so the event and the schedule advance commit together.
                values = _values(event)
                values["thread_key"] = thread.key
                values["status"] = EventStatus.ROUTED.value
                columns = ", ".join(values)
                placeholders = ", ".join(f":{name}" for name in values)
                inserted = db.execute(
                    f"INSERT INTO events ({columns}) VALUES ({placeholders}) "
                    "ON CONFLICT(idempotency_key) DO NOTHING", values,
                ).rowcount
                if inserted:
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

        fired = await self._call(write)
        if fired:
            self._wake.set()
            announce_routed(self, event, thread)
        return fired
