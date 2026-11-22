"""The host's one claim loop: due threads of every tenant, run fairly (step 3).

One `HostRunner` replaces the per-tenant dispatcher tasks. Each pass:

1. **Find due tenants** from the wake record (wakes.py): on a hub one indexed query on
   `handler_control.tenant_state`, never a tenant schema; locally an in-memory record.
   An in-process hint (a store's signal, or `NOTIFY handler_work_shared` naming a
   schema) makes a tenant a candidate at once; the poll is the backstop.
2. **Look inside each** (its context from the cache, one `work_state` query): which
   lanes `claim` would find now, and when it next needs a look (a retry's
   `available_at`, a live lease's `lease_until`, which a crash lets expire). A tenant
   with nothing claimable now is written back with that time.
3. **Admit** every (tenant, lane) with claimable work through the host `RunGate`,
   which ranks interactive before background across tenants, then paid before free,
   then fewer running, then the longest wait, with aging. Each one asks for its own
   free capacity (the tenant's lane capacity, settings `limits`), the gate grants
   what its ranking allows, and the threads are claimed with the existing `claim()`
   (FOR UPDATE SKIP LOCKED, a fenced lease). A lane that yields fewer threads than
   granted is drained; once a tenant has none left it is written back.
4. **Run** each lease as an asyncio task with the tenant's context pinned and the
   tenant set in the context variable. Lease renewal, fencing and the abandoned-run
   sweep are the executor's, as before.

Work waiting at the gate keeps its place (and its wait time) across passes. A slot
freeing wakes the loop at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .context import reset_current_tenant, set_current_tenant
from .tenancy.contexts import HostTenants, ServedTenant
from .tenancy.run_gate import RunGate
from .types import Lane, new_id
from .wakes import RUNS, ControlWakes, Due, LocalWakes

logger = logging.getLogger("handler.runtime.runner")

DEFAULT_POLL_SECONDS = 5.0
INSPECT_CONCURRENCY = 4
DEFAULT_LANES = {Lane.INTERACTIVE: 2, Lane.BACKGROUND: 1}


class HostRunner:
    def __init__(self, tenants: HostTenants, gate: RunGate | None = None, *,
                 poll_seconds: float = DEFAULT_POLL_SECONDS, owner: str | None = None,
                 local: LocalWakes | None = None) -> None:
        self.tenants = tenants
        self.gate = gate
        self.poll_seconds = poll_seconds
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{new_id('w')}"
        self.local = local or LocalWakes()
        self._control: dict[int, ControlWakes] = {}
        self._wake = asyncio.Event()
        self._freed = asyncio.Event()
        self._hinted: set[str] = set()
        self._hint_all = False
        self._want: dict[tuple[str, Lane], None] = {}  # insertion order: first seen
        self._rewrite: dict[str, tuple[Any, int | None, datetime | None]] = {}
        self._running: dict[tuple[str, Lane], int] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._caps: dict[str, tuple[dict[Lane, int], int | None]] = {}

    # --- signals ---------------------------------------------------------------
    def hint(self, name: str = "") -> None:
        """`name` may have new work ("" : any tenant may). Safe from any callback."""
        if name:
            self._hinted.add(name)
            self.local.mark(name)
        else:
            self._hint_all = True
        self._wake.set()

    def hint_schema(self, hub: Any, schema: str) -> None:
        if not schema:
            self.hint()
            return
        tenant = self.tenants.by_key(hub, schema)
        if tenant is not None:
            self.hint(tenant.name)

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def running(self, name: str) -> int:
        return sum(count for (tenant, _), count in self._running.items() if tenant == name)

    # --- one pass --------------------------------------------------------------
    def _source(self, tenant: ServedTenant):
        if tenant.hub is None:
            return self.local
        source = self._control.get(id(tenant.hub))
        if source is None:
            source = self._control[id(tenant.hub)] = ControlWakes(tenant.hub, RUNS)
        return source

    async def _due(self) -> list[tuple[Any, Due]]:
        served = {tenant.name: tenant for tenant in self.tenants.enabled()}
        groups: dict[int, tuple[Any, list[str]]] = {}
        for tenant in served.values():
            source = self._source(tenant)
            groups.setdefault(id(source), (source, []))[1].append(tenant.name)
        found: dict[str, tuple[Any, Due]] = {}
        for source, names in groups.values():
            for due in await source.due(names):
                found[due.name] = (source, due)
        hinted, self._hinted = self._hinted, set()
        if self._hint_all:
            self._hint_all = False
            hinted |= set(served)
        for name in hinted:
            if name in served and name not in found:
                found[name] = (self._source(served[name]), Due(name, None))
        # Drop work of tenants no longer served (disabled, removed).
        for key in [key for key in self._want if key[0] not in served]:
            self._unwant(key)
        return list(found.values())

    async def _inspect(self, source: Any, due: Due) -> None:
        async with self.tenants.use(due.name) as context:
            if context is None:
                return
            self._caps[due.name] = (dict(context.lane_capacity or DEFAULT_LANES),
                                    context.max_concurrent)
            state = await context.store.work_state()
        lanes = [lane for lane in (Lane.INTERACTIVE, Lane.BACKGROUND) if lane in state.lanes]
        for lane in lanes:
            self._want.setdefault((due.name, lane), None)
        if any(name == due.name for name, _ in self._want):
            self._rewrite[due.name] = (source, due.version, state.next_at)
        else:
            await source.reschedule(due.name, due.version, state.next_at)

    def _rank(self, key: tuple[str, Lane]):
        name, lane = key
        if self.gate is not None:
            return self.gate._key(name, lane)
        return (0 if lane == Lane.INTERACTIVE else 1, self.running(name))

    def _free(self, name: str, lane: Lane) -> int:
        lanes, max_concurrent = self._caps.get(name, (DEFAULT_LANES, None))
        free = lanes.get(lane, 0) - self._running.get((name, lane), 0)
        if max_concurrent is not None:
            free = min(free, max_concurrent - self.running(name))
        return free

    def _unwant(self, key: tuple[str, Lane]) -> None:
        self._want.pop(key, None)
        if self.gate is not None:
            self.gate.dequeue(*key)

    async def _settle(self, name: str, until: datetime | None) -> None:
        """No lane of `name` is waiting any more: write its next look back."""
        if any(tenant == name for tenant, _ in self._want):
            return
        pending = self._rewrite.pop(name, None)
        if pending is None:
            return
        source, version, at = pending
        if until is not None and (at is None or until < at):
            at = until
        with contextlib.suppress(Exception):
            await source.reschedule(name, version, at)

    async def _admit(self) -> int:
        started = 0
        if self.gate is not None:
            for key in self._want:
                self.gate.enqueue(key[0], self._freed, key[1])
        for key in sorted(self._want, key=self._rank):
            name, lane = key
            free = self._free(name, lane)
            if free <= 0:
                # At its own capacity: out of the gate's queue until one of its runs
                # ends (which wakes this loop), so it holds no one else back.
                if self.gate is not None:
                    self.gate.dequeue(name, lane)
                continue
            granted = self.gate.reserve(name, free, lane) if self.gate is not None else free
            if granted <= 0:
                continue
            leases: list = []
            try:
                async with self.tenants.use(name) as context:
                    if context is not None:
                        leases = await context.store.claim(self.owner, lane, granted,
                                                           context.lease_seconds)
            except Exception:
                logger.exception("claim failed", extra={"tenant": name, "lane": lane.value})
                leases = []
            finally:
                if self.gate is not None:
                    self.gate.release(name, granted - len(leases))
            for lease in leases:
                self._start(name, lane, lease)
                started += 1
            if len(leases) < granted:  # the lane is drained
                self._unwant(key)
                await self._settle(name, min((lease.until for lease in leases), default=None))
        return started

    async def claim_once(self) -> int:
        """One pass: find due tenants, look inside, admit and start runs."""
        self._freed.clear()
        self._wake.clear()
        self.tenants.cache.expire()
        due = await self._due()
        if due:
            limit = asyncio.Semaphore(INSPECT_CONCURRENCY)

            async def inspect(source: Any, item: Due) -> None:
                async with limit:
                    try:
                        await self._inspect(source, item)
                    except Exception:
                        logger.exception("could not look at a tenant's work",
                                         extra={"tenant": item.name})

            await asyncio.gather(*(inspect(source, item) for source, item in due))
        return await self._admit()

    # --- runs ------------------------------------------------------------------
    def _start(self, name: str, lane: Lane, lease: Any) -> None:
        self._running[(name, lane)] = self._running.get((name, lane), 0) + 1
        task = asyncio.create_task(self._execute(name, lease), name=f"run:{lease.thread_key}")
        self._tasks.add(task)
        task.add_done_callback(lambda t, name=name, lane=lane: self._on_done(name, lane, t))

    async def _execute(self, name: str, lease: Any) -> None:
        token = set_current_tenant(name)
        try:
            async with self.tenants.use(name) as context:
                if context is not None:
                    await context.executor.execute(lease)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("executor crashed for %s", lease.thread_key,
                             extra={"tenant": name})
        finally:
            reset_current_tenant(token)

    def _on_done(self, name: str, lane: Lane, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        self._running[(name, lane)] -= 1
        if not self._running[(name, lane)]:
            del self._running[(name, lane)]
        if self.gate is not None:
            self.gate.release(name)
        self.hint(name)  # its next pending thread, or a lane that was at capacity
        self._freed.set()

    # --- the loop --------------------------------------------------------------
    async def run(self, stop: asyncio.Event, shutdown_grace: float = 30.0) -> None:
        logger.info("claim loop started", extra={"owner": self.owner})
        try:
            while not stop.is_set():
                try:
                    if await self.claim_once():
                        continue  # more may be waiting; look again before sleeping
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("claim pass failed")
                waits = [asyncio.ensure_future(e.wait()) for e in (self._wake, self._freed, stop)]
                try:
                    await asyncio.wait(waits, timeout=self.poll_seconds,
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in waits:
                        waiter.cancel()
                    await asyncio.gather(*waits, return_exceptions=True)
        finally:
            await self.drain(shutdown_grace)
            if self.gate is not None:
                for key in list(self._want):
                    self.gate.dequeue(*key)
            logger.info("claim loop stopped")

    async def drain(self, grace: float) -> None:
        tasks = list(self._tasks)
        if not tasks:
            return
        logger.info("waiting for %d active run(s) to finish", len(tasks))
        _, pending = await asyncio.wait(tasks, timeout=grace)
        for task in pending:
            task.cancel()  # leases expire and another worker retries the thread
        if pending:
            await asyncio.wait(pending, timeout=5)


def runner_settings(env) -> dict[str, float]:
    return {"poll_seconds": float(env.get("HANDLER_CLAIM_POLL_SECONDS",
                                          str(DEFAULT_POLL_SECONDS)))}


Hint = Callable[[str], None]
