"""Process-wide run limits above each instance's own dispatcher capacity.

One gate is shared by every tenant's dispatcher. It enforces a global run cap and
a per-plan cap per tenant. Each dispatcher waits per lane. When slots are
contended, interactive work goes before background work across all tenants; then
paid tenants go first; then the tenant with the fewest runs; then the longest wait.

Nothing waits forever: a free tenant that has waited `max_wait` seconds ranks with
paid ones, and background work that has waited `max_wait` seconds goes ahead of
everything not yet overdue, oldest first. Its wait restarts once it gets a slot, so
under a constant stream of chats a tenant's background lane still gets one slot
every `max_wait` seconds or so.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping

from ..types import Lane

DEFAULT_MAX_RUNS = 6
DEFAULT_PLAN_CAPS = "free:1,paid:3"
_PAID = "paid"


def parse_plan_caps(raw: str) -> dict[str, int]:
    caps = {}
    for item in raw.split(","):
        plan, _, value = item.strip().partition(":")
        if not plan or int(value) < 1:
            raise ValueError(f"invalid plan cap: {item!r}")
        caps[plan] = int(value)
    return caps


class RunGate:
    def __init__(self, max_runs: int = DEFAULT_MAX_RUNS,
                 plan_caps: Mapping[str, int] | None = None, *,
                 max_wait: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        if max_runs < 1:
            raise ValueError("max_runs must be positive")
        self.max_runs = max_runs
        self.plan_caps = dict(plan_caps or parse_plan_caps(DEFAULT_PLAN_CAPS))
        self.max_wait = max_wait
        self._clock = clock
        self._plans: dict[str, str] = {}
        self._overrides: dict[str, int] = {}
        self._running: dict[str, int] = {}
        self._total = 0
        self._waiting: dict[tuple[str, Lane], tuple[float, asyncio.Event]] = {}

    def configure(self, tenant: str, plan: str, max_runs: int | None = None) -> None:
        self._plans[tenant] = plan
        if max_runs is None:
            self._overrides.pop(tenant, None)
        else:
            self._overrides[tenant] = max_runs

    def _cap(self, tenant: str) -> int:
        override = self._overrides.get(tenant)
        if override is not None:
            return override
        return self.plan_caps.get(self._plans.get(tenant, "free"), min(self.plan_caps.values()))

    def _key(self, tenant: str, lane: Lane) -> tuple[int, int, int, float]:
        waiting = self._waiting.get((tenant, lane))
        since = waiting[0] if waiting is not None else self._clock()
        aged = self._clock() - since >= self.max_wait
        if lane == Lane.INTERACTIVE:
            tier = 1
        else:
            tier = 0 if aged else 2  # overdue background goes first; else after every chat
        rank = 0 if self._plans.get(tenant) == _PAID or aged else 1
        return tier, rank, self._running.get(tenant, 0), since

    def running(self, tenant: str) -> int:
        return self._running.get(tenant, 0)

    def waiting(self, tenant: str, lane: Lane | None = None) -> bool:
        return any(t == tenant and (lane is None or lane == waiting_lane)
                   for t, waiting_lane in self._waiting)

    @property
    def total(self) -> int:
        return self._total

    def reserve(self, tenant: str, want: int, lane: Lane = Lane.INTERACTIVE) -> int:
        """Take up to `want` slots for `lane` now, leaving room for waiters ranked ahead."""
        running = self._running.get(tenant, 0)
        room = min(want, self._cap(tenant) - running, self.max_runs - self._total)
        if room <= 0:
            return 0
        mine = self._key(tenant, lane)
        ahead = sum(1 for other, other_lane in self._waiting if other != tenant
                    and self._key(other, other_lane) < mine
                    and self._running.get(other, 0) < self._cap(other))
        granted = min(room, self.max_runs - self._total - ahead)
        if granted <= 0:
            return 0
        self._running[tenant] = running + granted
        self._total += granted
        self._waiting.pop((tenant, lane), None)
        return granted

    def release(self, tenant: str, count: int = 1) -> None:
        if count <= 0:
            return
        self._running[tenant] = self._running.get(tenant, 0) - count
        self._total -= count
        for _, event in self._waiting.values():
            event.set()

    def enqueue(self, tenant: str, event: asyncio.Event,
                lane: Lane = Lane.INTERACTIVE) -> None:
        """Record that `tenant` has claimable work in `lane`; `event` is set when a slot frees."""
        if (tenant, lane) not in self._waiting:
            self._waiting[(tenant, lane)] = (self._clock(), event)

    def dequeue(self, tenant: str, lane: Lane | None = None) -> None:
        """Stop waiting in `lane` (every lane if None)."""
        gone = [key for key in self._waiting
                if key[0] == tenant and (lane is None or key[1] == lane)]
        if not gone:
            return  # no one was held back by this tenant
        for key in gone:
            del self._waiting[key]
        for _, event in self._waiting.values():
            event.set()
