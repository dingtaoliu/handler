"""Output sinks: durable delivery of domain events to integrations.

Core code emits domain events (`notification.created` today; `task.status_changed`
and others can follow) to the `Outbox`. Each registered `Sink` that `accepts`
an event gets its own row in the store's `deliveries` table, keyed by
(sink name, event key), so emitting the same event twice is a no-op. The host's
one `HostOutbox` loop claims due rows across tenants and calls
`sink.deliver(payload)`; failures retry by the sink's `RetryPolicy` and
dead-letter after `max_attempts`. A tenant's `Outbox` holds its sinks and holds
no task.

The outbox knows nothing about any one integration. A sink is registered only
when its integration is configured (see `integrations/crontal/sink.py`).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .types import utcnow

logger = logging.getLogger("handler.runtime.sinks")

NOTIFICATION_CREATED = "notification.created"


@dataclass(frozen=True)
class DomainEvent:
    type: str  # e.g. "notification.created"
    key: str  # idempotency key, unique per event (e.g. the notification id)
    data: dict[str, Any]
    occurred_at: datetime = field(default_factory=utcnow)

    def payload(self) -> dict[str, Any]:
        return {"type": self.type, "key": self.key, "data": self.data,
                "occurred_at": self.occurred_at.isoformat()}


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 10
    backoff_seconds: tuple[float, ...] = (10, 30, 120, 600, 1800, 3600)

    def retry_in(self, attempts: int) -> float | None:
        """Delay after the `attempts`-th failure, or None to dead-letter."""
        if attempts >= self.max_attempts:
            return None
        return self.backoff_seconds[min(attempts, len(self.backoff_seconds)) - 1]


class DeliveryError(Exception):
    """A failed delivery. retryable=False dead-letters at once (e.g. HTTP 400)."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Sink(Protocol):
    name: str
    retry: RetryPolicy

    def accepts(self, event: DomainEvent) -> bool: ...

    async def deliver(self, payload: dict[str, Any]) -> None:
        """Deliver one stored payload (DomainEvent.payload()); raise to retry."""
        ...


class DeliveryStore(Protocol):
    async def enqueue_delivery(self, sink: str, key: str, payload: dict[str, Any]) -> bool: ...

    async def claim_deliveries(self, sinks: list[str], limit: int = 20,
                               lease_seconds: float = 60.0) -> list[Any]: ...

    async def delivery_succeeded(self, sink: str, key: str) -> None: ...

    async def delivery_failed(self, sink: str, key: str, error: str,
                              retry_in_seconds: float | None) -> None: ...


class Outbox:
    def __init__(self, store: DeliveryStore, sinks: list[Sink] | None = None, *,
                 poll_seconds: float = 5.0, lease_seconds: float = 60.0,
                 tenant: str = "") -> None:
        self.store = store
        self.tenant = tenant or "main"
        self.sinks: dict[str, Sink] = {}
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self._wake = asyncio.Event()
        self.on_wake: Any = None  # the host outbox loop's hint for this tenant
        for sink in sinks or []:
            self.register(sink)

    def register(self, sink: Sink) -> None:
        if sink.name in self.sinks:
            raise ValueError(f"sink {sink.name!r} is already registered")
        self.sinks[sink.name] = sink

    async def emit(self, event: DomainEvent) -> list[str]:
        """Queue the event for every accepting sink. Never raises."""
        queued = []
        for sink in self.sinks.values():
            try:
                if sink.accepts(event) and await self.store.enqueue_delivery(
                        sink.name, event.key, event.payload()):
                    queued.append(sink.name)
            except Exception:
                logger.exception("enqueue failed", extra={"sink": sink.name,
                                                          "event": event.type, "key": event.key})
        if queued:
            self.wake()
        return queued

    def wake(self) -> None:
        """Deliver now (e.g. rows were just requeued)."""
        self._wake.set()
        if self.on_wake is not None:
            self.on_wake()

    async def deliver_once(self, limit: int = 20) -> int:
        """Deliver due rows once; returns how many succeeded."""
        return (await self.deliver_batch(limit))[1]

    async def deliver_batch(self, limit: int = 20) -> tuple[int, int]:
        """Deliver up to `limit` due rows; returns (claimed, succeeded)."""
        delivered = 0
        rows = await self.store.claim_deliveries(list(self.sinks), limit, self.lease_seconds)
        for row in rows:
            sink = self.sinks[row.sink]
            try:
                await sink.deliver(row.payload)
            except Exception as exc:
                attempts = row.attempts + 1
                retryable = not isinstance(exc, DeliveryError) or exc.retryable
                retry_in = sink.retry.retry_in(attempts) if retryable else None
                error = f"{type(exc).__name__}: {exc}"
                await self.store.delivery_failed(row.sink, row.key, error, retry_in)
                extra = {"sink": row.sink, "key": row.key, "attempts": attempts, "error": error[:500]}
                if retry_in is None:
                    # The stable message is the operator's log-based alert; the row
                    # is kept for redeliver. Nobody else is notified.
                    logger.error("delivery dead", extra={**extra, "tenant": self.tenant})
                else:
                    logger.warning("delivery failed, retrying", extra={**extra, "retry_in": retry_in})
                continue
            await self.store.delivery_succeeded(row.sink, row.key)
            delivered += 1
        return len(rows), delivered

    async def run_forever(self, stop: asyncio.Event) -> None:
        """This outbox alone, through the host loop (tests and tools)."""
        from .tenancy.contexts import ContextCache, HostTenants, ServedTenant, TenantContext

        cache = ContextCache()
        cache.pin(TenantContext(name=self.tenant, key="local", store=self.store, outbox=self))
        tenants = HostTenants(cache)
        tenants.serve(ServedTenant(self.tenant, "local"))
        loop = HostOutbox(tenants, poll_seconds=self.poll_seconds)
        loop.local.register(self.tenant)
        self.on_wake = lambda: loop.hint(self.tenant)
        await loop.run(stop)


DEFAULT_OUTBOX_POLL_SECONDS = 5.0
DEFAULT_DELIVERY_BATCH = 20
DEFAULT_OUTBOX_CONCURRENCY = 4


class HostOutbox:
    """The host's one delivery loop, across tenants (step 3).

    Due tenants come from the wake record (wakes.py: `tenant_state.next_delivery_at`
    on a hub, marked by every write that makes a delivery due). Each pass takes due
    tenants oldest first and delivers at most `batch` rows for each, at most
    `concurrency` tenants at once, then looks again: a tenant with a backlog gets one
    batch per pass, round robin, so it cannot hold the loop. A tenant is written back
    with its next due row (a retry's backoff) once its due rows are done. Sinks are
    the tenant's own, from its context. Per-tenant and per-sink limits are step 6.
    """

    def __init__(self, tenants: Any, *, poll_seconds: float = DEFAULT_OUTBOX_POLL_SECONDS,
                 batch: int = DEFAULT_DELIVERY_BATCH,
                 concurrency: int = DEFAULT_OUTBOX_CONCURRENCY) -> None:
        from .wakes import LocalWakes

        self.tenants = tenants
        self.poll_seconds = poll_seconds
        self.batch = max(1, batch)
        self.concurrency = max(1, concurrency)
        self.local = LocalWakes()
        self._control: dict[int, Any] = {}
        self._hinted: set[str] = set()
        self._wake = asyncio.Event()

    def hint(self, name: str = "") -> None:
        if name:
            self._hinted.add(name)
            self.local.mark(name)
        self._wake.set()

    def _source(self, tenant: Any) -> Any:
        from .wakes import DELIVERIES, ControlWakes

        if tenant.hub is None:
            return self.local
        source = self._control.get(id(tenant.hub))
        if source is None:
            source = self._control[id(tenant.hub)] = ControlWakes(tenant.hub, DELIVERIES)
        return source

    async def deliver_once(self) -> int:
        """One round: up to `batch` rows for each due tenant. Returns rows claimed."""
        from .wakes import Due

        self._wake.clear()
        served = {tenant.name: tenant for tenant in self.tenants.enabled()}
        groups: dict[int, tuple[Any, list[str]]] = {}
        for tenant in served.values():
            source = self._source(tenant)
            groups.setdefault(id(source), (source, []))[1].append(tenant.name)
        due: dict[str, tuple[Any, Any]] = {}
        for source, names in groups.values():
            for item in await source.due(names):
                due[item.name] = (source, item)
        hinted, self._hinted = self._hinted, set()
        for name in hinted:
            if name in served and name not in due:
                due[name] = (self._source(served[name]), Due(name, None))
        limit = asyncio.Semaphore(self.concurrency)
        claimed = 0

        async def one(source: Any, item: Any) -> None:
            nonlocal claimed
            async with limit:
                try:
                    async with self.tenants.use(item.name) as context:
                        outbox = getattr(context, "outbox", None) if context else None
                        if outbox is None or not outbox.sinks:
                            await source.reschedule(item.name, item.version, None)
                            return
                        taken, _ = await outbox.deliver_batch(self.batch)
                        claimed += taken
                        if taken >= self.batch:
                            return  # more may be due: next round, after the others
                        at = await context.store.next_delivery_at(list(outbox.sinks))
                    await source.reschedule(item.name, item.version, at)
                except Exception:
                    logger.exception("outbox pass failed", extra={"tenant": item.name})

        await asyncio.gather(*(one(source, item) for source, item in due.values()))
        return claimed

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                if await self.deliver_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("outbox pass failed")
            waits = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(self._wake.wait())]
            try:
                await asyncio.wait(waits, timeout=self.poll_seconds,
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waits:
                    waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*waits, return_exceptions=True)


def outbox_settings(env) -> dict[str, float]:
    return {"poll_seconds": float(env.get("HANDLER_OUTBOX_POLL_SECONDS",
                                          str(DEFAULT_OUTBOX_POLL_SECONDS))),
            "batch": int(env.get("HANDLER_OUTBOX_BATCH", str(DEFAULT_DELIVERY_BATCH))),
            "concurrency": int(env.get("HANDLER_OUTBOX_CONCURRENCY",
                                       str(DEFAULT_OUTBOX_CONCURRENCY)))}
