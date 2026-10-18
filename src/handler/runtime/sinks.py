"""Output sinks: durable delivery of domain events to integrations.

Core code emits domain events (`notification.created` today; `task.status_changed`
and others can follow) to the `Outbox`. Each registered `Sink` that `accepts`
an event gets its own row in the store's `deliveries` table, keyed by
(sink name, event key), so emitting the same event twice is a no-op. A
background worker claims due rows and calls `sink.deliver(payload)`; failures
retry by the sink's `RetryPolicy` and dead-letter after `max_attempts`.

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
                 poll_seconds: float = 5.0, lease_seconds: float = 60.0) -> None:
        self.store = store
        self.sinks: dict[str, Sink] = {}
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self._wake = asyncio.Event()
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
            self._wake.set()
        return queued

    async def deliver_once(self, limit: int = 20) -> int:
        """Deliver due rows once; returns how many succeeded."""
        delivered = 0
        for row in await self.store.claim_deliveries(list(self.sinks), limit, self.lease_seconds):
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
                    logger.error("delivery dead-lettered", extra=extra)
                else:
                    logger.warning("delivery failed, retrying", extra={**extra, "retry_in": retry_in})
                continue
            await self.store.delivery_succeeded(row.sink, row.key)
            delivered += 1
        return delivered

    async def run_forever(self, stop: asyncio.Event) -> None:
        if not self.sinks:
            return
        while not stop.is_set():
            self._wake.clear()
            try:
                await self.deliver_once()
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
