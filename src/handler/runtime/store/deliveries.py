"""Durable delivery outbox for output sinks, on both stores.

One row per (sink, key): the sink's name and the domain event's idempotency
key. Rows are claimed by moving `next_attempt_at` forward (a short lease), so
a crashed worker's rows become due again on their own. The outbox worker in
`runtime/sinks.py` is the only writer besides `enqueue_delivery`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

PENDING, DELIVERED, DEAD = "pending", "delivered", "dead"

MARK_DELIVERY_SQL = (
    "UPDATE handler_control.tenant_state SET next_delivery_at = "
    "LEAST(next_delivery_at, %s), delivery_version = delivery_version + 1 "
    "WHERE schema_name = %s")


@dataclass(frozen=True)
class Delivery:
    sink: str
    key: str
    payload: dict[str, Any]
    status: str
    attempts: int
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None
    delivered_at: datetime | None = None


def _date(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _delivery(row: Any) -> Delivery:
    row = dict(row)
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return Delivery(
        sink=row["sink"], key=row["key"], payload=payload, status=row["status"],
        attempts=int(row["attempts"]), next_attempt_at=_date(row["next_attempt_at"]),
        created_at=_date(row["created_at"]), updated_at=_date(row["updated_at"]),
        last_error=row["last_error"], delivered_at=_date(row["delivered_at"]),
    )


class _Deliveries:
    """Backend-neutral SQL; subclasses supply _dq (query) and _ts (timestamp).

    A write that makes a row due (`mark`: when) tells the host's outbox loop: in its
    transaction on a hub (tenant_state.next_delivery_at), and in process through
    `on_delivery_due`.
    """

    on_delivery_due = None  # Callable[[], None]: the host outbox loop's hint

    def _delivery_due(self) -> None:
        if self.on_delivery_due is not None:
            self.on_delivery_due()

    async def enqueue_delivery(self, sink: str, key: str, payload: dict[str, Any]) -> bool:
        """Queue one delivery; False when (sink, key) already exists (idempotent)."""
        now = self._ts(self._clock())
        rows = await self._dq(
            "INSERT INTO deliveries (sink, key, payload, status, attempts, next_attempt_at, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?) "
            "ON CONFLICT (sink, key) DO NOTHING RETURNING key",
            (sink, key, self._json(payload), PENDING, now, now, now), mark=self._clock(),
        )
        if rows:
            self._delivery_due()
        return bool(rows)

    async def get_delivery(self, sink: str, key: str) -> Delivery | None:
        rows = await self._dq("SELECT * FROM deliveries WHERE sink = ? AND key = ?", (sink, key))
        return _delivery(rows[0]) if rows else None

    async def list_deliveries(self, status: str | None = None, limit: int = 100) -> list[Delivery]:
        where, params = ("WHERE status = ? ", (status,)) if status else ("", ())
        rows = await self._dq(
            f"SELECT * FROM deliveries {where}ORDER BY created_at DESC LIMIT ?",
            (*params, max(0, limit)),
        )
        return [_delivery(row) for row in rows]

    async def delivery_succeeded(self, sink: str, key: str) -> None:
        now = self._ts(self._clock())
        await self._dq(
            "UPDATE deliveries SET status = ?, attempts = attempts + 1, last_error = NULL, "
            "delivered_at = ?, updated_at = ? WHERE sink = ? AND key = ? RETURNING key",
            (DELIVERED, now, now, sink, key),
        )

    async def delivery_failed(
        self, sink: str, key: str, error: str, retry_in_seconds: float | None
    ) -> None:
        """Record a failed attempt: retry after the delay, or dead-letter (None)."""
        now = self._clock()
        status = PENDING if retry_in_seconds is not None else DEAD
        due = now + timedelta(seconds=retry_in_seconds or 0)
        await self._dq(
            "UPDATE deliveries SET status = ?, attempts = attempts + 1, last_error = ?, "
            "next_attempt_at = ?, updated_at = ? WHERE sink = ? AND key = ? RETURNING key",
            (status, error[:2000], self._ts(due), self._ts(now), sink, key),
            mark=due if retry_in_seconds is not None else None,
        )


    async def redeliver(self, sink: str, key: str | None = None) -> int:
        """Requeue dead rows of `sink` (one `key`, or all of them); returns how many.

        Each gets a fresh set of attempts and is due at once. Pending and
        delivered rows are left alone. last_error stays until the next attempt.
        """
        now = self._ts(self._clock())
        where, params = ("AND key = ? ", (key,)) if key is not None else ("", ())
        rows = await self._dq(
            "UPDATE deliveries SET status = ?, attempts = 0, next_attempt_at = ?, "
            f"updated_at = ? WHERE sink = ? AND status = ? {where}RETURNING key",
            (PENDING, now, now, sink, DEAD, *params), mark=self._clock(),
        )
        if rows:
            self._delivery_due()
        return len(rows)

    async def next_delivery_at(self, sinks: list[str]) -> datetime | None:
        """When the earliest pending row of these sinks is due (the outbox loop)."""
        if not sinks:
            return None
        marks = ",".join("?" for _ in sinks)
        rows = await self._dq(
            f"SELECT MIN(next_attempt_at) AS due FROM deliveries WHERE status = ? "
            f"AND sink IN ({marks})", (PENDING, *sinks))
        return _date(rows[0]["due"]) if rows else None


class SQLiteDeliveries(_Deliveries):
    def _ts(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def _json(self, value: dict) -> str:
        return json.dumps(value, default=str)

    async def _dq(self, query: str, parameters: tuple, mark=None) -> list[dict]:
        def run(db):
            return [dict(row) for row in db.execute(query, parameters)]
        return await self._call(run)

    async def claim_deliveries(
        self, sinks: list[str], limit: int = 20, lease_seconds: float = 60.0
    ) -> list[Delivery]:
        """Due pending rows of the given sinks, leased for `lease_seconds`."""
        if not sinks or limit <= 0:
            return []
        from .sqlite import _transaction

        now = self._clock()
        marks = ",".join("?" for _ in sinks)

        def claim(db):
            with _transaction(db):
                rows = [dict(row) for row in db.execute(
                    f"SELECT * FROM deliveries WHERE status = ? AND sink IN ({marks}) "
                    "AND next_attempt_at <= ? ORDER BY next_attempt_at, created_at LIMIT ?",
                    (PENDING, *sinks, self._ts(now), limit),
                )]
                until = self._ts(now + timedelta(seconds=lease_seconds))
                for row in rows:
                    db.execute("UPDATE deliveries SET next_attempt_at = ? "
                               "WHERE sink = ? AND key = ?", (until, row["sink"], row["key"]))
                return rows
        return [_delivery(row) for row in await self._call(claim)]


class PostgresDeliveries(_Deliveries):
    def _ts(self, value: datetime) -> datetime:
        return value

    def _json(self, value: dict):
        from psycopg.types.json import Jsonb
        return Jsonb(value)

    async def _dq(self, query: str, parameters: tuple, mark=None) -> list[dict]:
        async with self._conn() as conn:
            cursor = await conn.execute(query.replace("?", "%s"), parameters)
            rows = await cursor.fetchall() if cursor.description else []
            if mark is not None and rows and self._hub is not None:
                # Commits with the row: the outbox loop finds it from handler_control.
                await conn.execute(MARK_DELIVERY_SQL, (mark, self._tenant_schema))
            return rows

    async def claim_deliveries(
        self, sinks: list[str], limit: int = 20, lease_seconds: float = 60.0
    ) -> list[Delivery]:
        if not sinks or limit <= 0:
            return []
        now = self._clock()
        async with self._conn() as conn:
            cursor = await conn.execute(
                "UPDATE deliveries d SET next_attempt_at = %s FROM ("
                "  SELECT sink, key FROM deliveries WHERE status = %s AND sink = ANY(%s) "
                "  AND next_attempt_at <= %s ORDER BY next_attempt_at, created_at LIMIT %s "
                "  FOR UPDATE SKIP LOCKED) due "
                "WHERE d.sink = due.sink AND d.key = due.key RETURNING d.*",
                (now + timedelta(seconds=lease_seconds), PENDING, list(sinks), now, limit),
            )
            rows = await cursor.fetchall()
        # RETURNING shows the leased time; callers only need the payload and attempts.
        return sorted((_delivery(row) for row in rows), key=lambda d: d.created_at)
