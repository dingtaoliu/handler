"""When each tenant next needs a host loop: the claim loop or the outbox loop.

On a hub the record is `handler_control.tenant_state` (`next_wake_at` and
`wake_version` for runs, `next_delivery_at` and `delivery_version` for deliveries).
Every writer of pending work marks it in its own transaction, keeping the earliest
time and bumping the version. A loop reads due tenants from that table alone (an
indexed query, no tenant schema), looks inside each one, and writes back when it next
needs a look, but only if no writer came in between (the version is unchanged): a
wake is never lost to a race.

A store on its own connections (SQLite, or a lone Postgres instance) has no control
schema; `LocalWakes` keeps the same record in memory, marked by the store's in-process
signal. Same loop, one tenant.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .store.postgres_hub import CONTROL_SCHEMA
from .types import utcnow

RUNS = ("next_wake_at", "wake_version")
DELIVERIES = ("next_delivery_at", "delivery_version")


@dataclass(frozen=True)
class Due:
    name: str
    version: int | None  # None: an in-process hint only (nothing to write back against)
    since: datetime | None = None


class ControlWakes:
    """The tenant_state columns of one hub, for one loop."""

    def __init__(self, hub: Any, columns: tuple[str, str] = RUNS) -> None:
        self.hub = hub
        self.at, self.version = columns

    async def due(self, names: Sequence[str], limit: int = 500) -> list[Due]:
        if not names:
            return []
        await self.hub.start()
        async with self.hub.pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT name, {self.version} AS version, {self.at} AS since "
                f"FROM {CONTROL_SCHEMA}.tenant_state WHERE {self.at} <= now() "
                f"AND name = ANY(%s) ORDER BY {self.at} LIMIT %s", (list(names), limit))
            return [Due(row["name"], row["version"], row["since"])
                    for row in await cursor.fetchall()]

    async def reschedule(self, name: str, version: int | None, at: datetime | None) -> bool:
        """Next look at `at` (None: only when a writer marks it). False if a writer
        came after the read: the tenant stays due."""
        async with self.hub.pool.connection() as conn:
            if version is None:
                if at is None:
                    return False
                cursor = await conn.execute(
                    f"UPDATE {CONTROL_SCHEMA}.tenant_state SET {self.at} = "
                    f"LEAST({self.at}, %s) WHERE name = %s", (at, name))
            else:
                cursor = await conn.execute(
                    f"UPDATE {CONTROL_SCHEMA}.tenant_state SET {self.at} = %s "
                    f"WHERE name = %s AND {self.version} = %s", (at, name, version))
            return bool(cursor.rowcount)


class LocalWakes:
    """The same record in memory, for tenants whose store has its own connections."""

    def __init__(self) -> None:
        self._at: dict[str, datetime | None] = {}
        self._version: dict[str, int] = {}
        self._clocks: dict[str, Callable[[], datetime]] = {}

    def register(self, name: str, clock: Callable[[], datetime] | None = None) -> None:
        self._clocks[name] = clock or utcnow
        self.mark(name)  # looked at once when first served

    def unregister(self, name: str) -> None:
        for table in (self._at, self._version, self._clocks):
            table.pop(name, None)

    def mark(self, name: str) -> None:
        clock = self._clocks.get(name)
        if clock is None:
            return
        self._version[name] = self._version.get(name, 0) + 1
        now, at = clock(), self._at.get(name)
        self._at[name] = now if at is None or at > now else at

    async def due(self, names: Sequence[str], limit: int = 500) -> list[Due]:
        found = []
        for name in names:
            at, clock = self._at.get(name), self._clocks.get(name)
            if at is not None and clock is not None and at <= clock():
                found.append(Due(name, self._version.get(name, 0), at))
        return sorted(found, key=lambda due: due.since)[:limit]

    async def reschedule(self, name: str, version: int | None, at: datetime | None) -> bool:
        if version is not None and self._version.get(name) != version:
            return False
        if version is None:
            current = self._at.get(name)
            if at is None or (current is not None and current <= at):
                return False
        self._at[name] = at
        return True
