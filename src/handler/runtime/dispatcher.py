"""Dispatcher: claims threads with pending work and runs them, per lane.

Event-driven: it sleeps on the store's wake signal (or a slot freeing up) and
never polls faster than `idle_poll_seconds`. Concurrency is capped per lane so
background work can't starve interactive chat.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

from .run import RunExecutor
from .store.base import Store
from .tenancy.run_gate import RunGate
from .types import Lane, new_id

logger = logging.getLogger("handler.runtime.dispatcher")


class Dispatcher:
    def __init__(
        self,
        store: Store,
        executor: RunExecutor,
        *,
        lane_capacity: dict[Lane, int] | None = None,
        max_concurrent: int | None = None,
        lease_seconds: float = 120.0,
        idle_poll_seconds: float = 30.0,
        owner: str | None = None,
        gate: RunGate | None = None,
        gate_tenant: str = "",
    ) -> None:
        if max_concurrent is not None and max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        self.max_concurrent = max_concurrent
        self.store = store
        self.executor = executor
        self.capacity = lane_capacity or {Lane.INTERACTIVE: 2, Lane.BACKGROUND: 1}
        self.lease_seconds = lease_seconds
        self.idle_poll_seconds = idle_poll_seconds
        self.gate = gate
        self.gate_tenant = gate_tenant
        self._queued_at_gate: set[Lane] = set()
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{new_id('w')}"
        self._active: dict[Lane, set[asyncio.Task[None]]] = {lane: set() for lane in self.capacity}
        self._slot_freed = asyncio.Event()

    @property
    def active_count(self) -> int:
        return sum(len(t) for t in self._active.values())

    async def claim_once(self) -> int:
        """Fill free slots in every lane. Returns the number of runs started."""
        # Cleared here, not before sleeping: a slot freed while this pass awaits the
        # store must still wake the next wait, or the work waits for the idle poll.
        self._slot_freed.clear()
        started = 0
        self._queued_at_gate = set()
        for lane, cap in self.capacity.items():
            free = cap - len(self._active[lane])
            if self.max_concurrent is not None:
                free = min(free, self.max_concurrent - self.active_count)
            if free <= 0:
                continue
            if self.gate is not None:
                free = await self._reserve(lane, free)
                if free <= 0:
                    continue
            try:
                leases = await self.store.claim(self.owner, lane, free, self.lease_seconds)
            except BaseException:
                if self.gate is not None:  # nothing was claimed: give every slot back
                    self.gate.release(self.gate_tenant, free)
                raise
            if self.gate is not None:
                self.gate.release(self.gate_tenant, free - len(leases))
            for lease in leases:
                task = asyncio.create_task(self._execute(lease), name=f"run:{lease.thread_key}")
                self._active[lane].add(task)
                task.add_done_callback(lambda t, lane=lane: self._on_done(lane, t))
                started += 1
        if self.gate is not None:
            for lane in self.capacity:
                if lane not in self._queued_at_gate:
                    self.gate.dequeue(self.gate_tenant, lane)  # this lane isn't waiting any more
        return started

    async def _reserve(self, lane: Lane, want: int) -> int:
        """Slots from the process-wide gate; if none, queue for one when work is waiting.

        The lane goes to the gate, which ranks interactive work before background
        work across every tenant."""
        granted = self.gate.reserve(self.gate_tenant, want, lane)
        if granted or not await self.store.has_claimable(lane):
            return granted
        self._queued_at_gate.add(lane)
        self.gate.enqueue(self.gate_tenant, self._slot_freed, lane)
        return self.gate.reserve(self.gate_tenant, want, lane)  # a slot may have freed meanwhile

    async def _execute(self, lease) -> None:
        try:
            await self.executor.execute(lease)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("executor crashed for %s", lease.thread_key)

    def _on_done(self, lane: Lane, task: asyncio.Task[None]) -> None:
        self._active[lane].discard(task)
        if self.gate is not None:
            self.gate.release(self.gate_tenant)
        self._slot_freed.set()

    async def run_forever(self, stop: asyncio.Event, shutdown_grace: float = 30.0) -> None:
        logger.info("dispatcher started", extra={"owner": self.owner})
        try:
            while not stop.is_set():
                if await self.claim_once():
                    continue  # more work may be waiting; claim again before sleeping
                await self._wait(stop)
        finally:
            await self._drain(shutdown_grace)
            if self.gate is not None:
                self.gate.dequeue(self.gate_tenant)
            logger.info("dispatcher stopped")

    async def _wait(self, stop: asyncio.Event) -> None:
        waiters = [
            asyncio.create_task(self.store.wait_for_work(self.idle_poll_seconds)),
            asyncio.create_task(self._slot_freed.wait()),
            asyncio.create_task(stop.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()

    async def _drain(self, grace: float) -> None:
        tasks = [t for ts in self._active.values() for t in ts]
        if not tasks:
            return
        logger.info("waiting for %d active run(s) to finish", len(tasks))
        _, pending = await asyncio.wait(tasks, timeout=grace)
        for t in pending:
            t.cancel()  # leases expire and another worker retries the thread
        if pending:
            await asyncio.wait(pending, timeout=5)
