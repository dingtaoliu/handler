"""In-process pub/sub for live run events (web UI streaming).

Best effort only: nothing here is durable. The durable record is the store.

One `RunEventBus` serves the whole host, keyed by (tenant, thread). Each tenant's
code holds a `TenantBus` view of it, which stamps everything it publishes with the
tenant and subscribes only to that tenant's keys, so an SSE stream can never see
another tenant's events, "*" (all threads) included.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator

from .types import utcnow


@dataclass(frozen=True)
class BusMessage:
    thread_key: str
    run_id: str | None
    type: str  # "run_started" | "run_event" | "run_finished" | "event_queued"
    data: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: utcnow().isoformat())
    tenant: str = ""  # set by the publishing tenant's TenantBus


class RunEventBus:
    """The host's bus. Subscriptions are per (tenant, thread key or "*")."""

    def __init__(self, max_queue: int = 1000) -> None:
        self._subs: dict[tuple[str, str], set[asyncio.Queue[BusMessage]]] = defaultdict(set)
        self._max_queue = max_queue
        self._views: dict[str, TenantBus] = {}

    def for_tenant(self, tenant: str) -> "TenantBus":
        view = self._views.get(tenant)
        if view is None:
            view = self._views[tenant] = TenantBus(self, tenant)
        return view

    def publish(self, msg: BusMessage) -> None:
        for key in ((msg.tenant, msg.thread_key), (msg.tenant, "*")):
            for q in list(self._subs.get(key, ())):
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    pass  # slow subscriber: drop rather than block runs

    @asynccontextmanager
    async def subscribe(self, thread_key: str = "*", *,
                        tenant: str = "") -> AsyncIterator[asyncio.Queue[BusMessage]]:
        """Subscribe to one tenant's thread, or "*" for all of that tenant's threads."""
        key = (tenant, thread_key)
        q: asyncio.Queue[BusMessage] = asyncio.Queue(self._max_queue)
        self._subs[key].add(q)
        try:
            yield q
        finally:
            self._subs[key].discard(q)
            if not self._subs[key]:
                self._subs.pop(key, None)

    def subscriber_count(self, tenant: str | None = None) -> int:
        return sum(len(qs) for (t, _), qs in self._subs.items() if tenant in (None, t))


class TenantBus:
    """One tenant's view of the host bus: what every runtime object is handed."""

    def __init__(self, bus: RunEventBus, tenant: str) -> None:
        self.host = bus
        self.tenant = tenant

    def publish(self, msg: BusMessage) -> None:
        if msg.tenant != self.tenant:
            msg = replace(msg, tenant=self.tenant)
        self.host.publish(msg)

    def subscribe(self, thread_key: str = "*"):
        return self.host.subscribe(thread_key, tenant=self.tenant)
