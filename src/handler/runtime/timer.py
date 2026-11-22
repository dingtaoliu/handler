"""The host's one timer: every tenant's schedules, mail polls, expiries and cleanup.

Time-driven work is a row in due work (store/due_work.py), written in the same
transaction as the work it describes. One `HostTimer` for the whole host claims due
rows, resolves the row's tenant through `TenantDirectory.tenant` (the one place a
tenant is looked up: today an active tenant's runtime objects; step 3 swaps in the
context cache), and calls the handler registered for the row's kind. The handler
does one firing and returns the next due time, or None when nothing more is due
(the row is removed). If the work's own transaction rewrote the row meanwhile (its
version changed), the stored due time wins.

A failure is recorded on the row (`attempts`, `last_error`) with backoff, and logged;
it never stops the timer. The timer sleeps until the earliest due time, woken early
by an in-process hint from the stores, by `NOTIFY handler_due_work` from other
processes (through the Postgres hub's listener), and at worst by a poll.

An idle tenant has no task here, and costs no tenant-scoped query: on Postgres the
claim and the earliest-due read touch only `handler_control.due_work`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from .context import reset_current_tenant, set_current_tenant
from .integrations.base import TimerKind
from .store.due_work import DueItem, DueQueue
from .types import utcnow

logger = logging.getLogger("handler.runtime.timer")

DEFAULT_POLL_SECONDS = 60.0
DEFAULT_TIMEOUT_SECONDS = 120.0
UNAVAILABLE_RETRY_SECONDS = 300.0  # a tenant that can't be resolved (disabled, at capacity)
MAX_BACKOFF_SECONDS = 3600.0

Handler = Callable[[DueItem], Awaitable[datetime | None]]


@dataclass
class TimerTarget:
    """One tenant as the timer sees it: its handlers, bound to its own objects."""

    name: str
    key: str  # the due-work tenant key (schema name, or "local" for SQLite)
    queue: DueQueue
    handlers: dict[str, Handler] = field(default_factory=dict)
    timeouts: dict[str, float] = field(default_factory=dict)

    def bind(self, kind: TimerKind, context: Any) -> None:
        if kind.name in self.handlers:
            raise ValueError(f"{self.name or 'instance'}: timer kind {kind.name!r} "
                             "is registered twice")

        async def run(item: DueItem, kind=kind, context=context):
            return await kind.handler(context, item)

        self.handlers[kind.name] = run
        self.timeouts[kind.name] = kind.timeout_seconds


class Directory(Protocol):
    def sources(self) -> list[tuple[DueQueue, list[str]]]:
        """Each due queue with the tenant keys this host serves from it."""
        ...

    def tenant(self, queue: DueQueue,
               key: str) -> contextlib.AbstractAsyncContextManager[TimerTarget | None]:
        """The tenant of a row from `queue`, held for the handler's duration (None:
        unavailable). The key is unique per queue: every SQLite file says "local"."""
        ...


class TenantDirectory:
    """Running tenants of an env-defined host (or a lone instance), by due key."""

    def __init__(self) -> None:
        self._targets: dict[tuple[int, str], TimerTarget] = {}

    def register(self, target: TimerTarget) -> None:
        key = (id(target.queue), target.key)
        if key in self._targets:
            raise ValueError(f"due-work tenant {target.key!r} is already registered")
        self._targets[key] = target

    def unregister(self, target: TimerTarget) -> None:
        self._targets.pop((id(target.queue), target.key), None)

    def sources(self) -> list[tuple[DueQueue, list[str]]]:
        grouped: dict[int, tuple[DueQueue, list[str]]] = {}
        for (queue_id, key), target in self._targets.items():
            grouped.setdefault(queue_id, (target.queue, []))[1].append(key)
        return list(grouped.values())

    @contextlib.asynccontextmanager
    async def tenant(self, queue: DueQueue, key: str) -> AsyncIterator[TimerTarget | None]:
        yield self._targets.get((id(queue), key))


def backoff_seconds(attempts: int) -> float:
    return min(30.0 * 2 ** min(max(attempts - 1, 0), 10), MAX_BACKOFF_SECONDS)


class HostTimer:
    def __init__(self, directory: Directory, *, poll_seconds: float = DEFAULT_POLL_SECONDS,
                 concurrency: int = 32, lease_seconds: float = 120.0,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.directory = directory
        self.poll_seconds = poll_seconds
        self.concurrency = max(1, concurrency)
        self.lease_seconds = lease_seconds
        self.clock = clock
        self.owner = f"timer:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
        self._wake = asyncio.Event()
        self._freed = asyncio.Event()
        self._running: dict[tuple[str, str, str], asyncio.Task[None]] = {}
        self._queues: dict[int, DueQueue] = {}

    def wake(self) -> None:
        """Due work changed: look again now (safe to call from any task)."""
        self._wake.set()

    # --- one pass -------------------------------------------------------------
    async def _claim(self) -> int:
        started = 0
        for queue, tenants in self.directory.sources():
            free = self.concurrency - len(self._running)
            if free <= 0:
                break
            self._queues[id(queue)] = queue
            for item in await queue.claim(self.owner, tenants, self.clock(), free,
                                          self.lease_seconds):
                key = (item.tenant, item.kind, item.ref)
                task = asyncio.create_task(self._fire(queue, item),
                                           name=f"due:{item.kind}:{item.tenant}")
                self._running[key] = task
                task.add_done_callback(lambda _t, key=key: self._done(key))
                started += 1
        return started

    def _done(self, key: tuple[str, str, str]) -> None:
        self._running.pop(key, None)
        self._freed.set()

    async def _next_due(self) -> datetime | None:
        earliest = None
        for queue, tenants in self.directory.sources():
            due = await queue.next_due(tenants, self.clock())
            if due is not None and (earliest is None or due < earliest):
                earliest = due
        return earliest

    async def _fire(self, queue: DueQueue, item: DueItem) -> None:
        extra = {"tenant": item.tenant, "kind": item.kind, "ref": item.ref}
        try:
            async with self.directory.tenant(queue, item.tenant) as target:
                if target is None:
                    await queue.postpone(item, self.owner, self.clock()
                                         + timedelta(seconds=UNAVAILABLE_RETRY_SECONDS))
                    return
                handler = target.handlers.get(item.kind)
                if handler is None:
                    # e.g. an integration that is no longer enabled for this tenant;
                    # its backfill recreates the rows if it comes back.
                    logger.info("due work of an unknown kind removed", extra=extra)
                    await queue.finish(item, self.owner, None, self.clock())
                    return
                token = set_current_tenant(target.name or "main")
                try:
                    next_due = await asyncio.wait_for(
                        handler(item), target.timeouts.get(item.kind, DEFAULT_TIMEOUT_SECONDS))
                finally:
                    reset_current_tenant(token)
                await queue.finish(item, self.owner, next_due, self.clock())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # recorded on the row; the timer carries on
            attempts = item.attempts + 1
            delay = backoff_seconds(attempts)
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            logger.warning("due work failed", exc_info=not isinstance(exc, TimeoutError),
                           extra={**extra, "attempts": attempts, "error": error,
                                  "retry_in": delay})
            with contextlib.suppress(Exception):
                now = self.clock()
                await queue.fail(item, self.owner, error, now + timedelta(seconds=delay), now)

    async def run_due(self) -> int:
        """Fire everything due now and wait for it (tests and the CLI)."""
        total = 0
        while True:
            started = await self._claim()
            if self._running:
                await asyncio.gather(*list(self._running.values()), return_exceptions=True)
            total += started
            if not started:
                return total

    # --- the loop --------------------------------------------------------------
    async def _renew(self) -> None:
        """Keep the leases of rows whose handler is still running."""
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            if not self._running:
                continue
            until = self.clock() + timedelta(seconds=self.lease_seconds)
            for queue in list(self._queues.values()):
                try:
                    await queue.renew(self.owner, until)
                except Exception:
                    logger.warning("due work lease renewal failed", exc_info=True)

    async def run(self, stop: asyncio.Event) -> None:
        renewer = asyncio.create_task(self._renew(), name="timer-lease")
        try:
            while not stop.is_set():
                self._wake.clear()
                self._freed.clear()
                delay = self.poll_seconds
                try:
                    await self._claim()
                    due = await self._next_due()
                    if due is not None:
                        delay = min(delay, max(0.0, (due - self.clock()).total_seconds()))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("timer pass failed")
                    delay = min(delay, 5.0)
                if len(self._running) >= self.concurrency:
                    delay = self.poll_seconds  # a finishing handler wakes us (_freed)
                waits = [asyncio.ensure_future(e.wait()) for e in (self._wake, self._freed, stop)]
                try:
                    await asyncio.wait(waits, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in waits:
                        waiter.cancel()
                    await asyncio.gather(*waits, return_exceptions=True)
        finally:
            renewer.cancel()
            running = list(self._running.values())
            for task in running:
                task.cancel()
            await asyncio.gather(renewer, *running, return_exceptions=True)
            for queue in list(self._queues.values()):
                with contextlib.suppress(Exception):
                    await queue.release(self.owner)  # due again at once after a restart


def timer_settings(env: Mapping[str, str]) -> dict[str, float]:
    return {"poll_seconds": float(env.get("HANDLER_TIMER_POLL_SECONDS",
                                          str(DEFAULT_POLL_SECONDS)))}
