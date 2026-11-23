"""One store's claims, through the host's claim loop (runner.py) with one tenant.

The host runs every tenant's threads from one `HostRunner`. `Dispatcher` is that loop
over a single store, for the CLI, tests and tools that drive one store directly: the
same code path, a host with one tenant.
"""

from __future__ import annotations

import asyncio

from .run import RunExecutor
from .runner import HostRunner
from .store.base import Store
from .tenancy.contexts import ContextCache, HostTenants, ServedTenant, TenantContext
from .tenancy.run_gate import RunGate
from .types import Lane


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
        self.store = store
        self.executor = executor
        self.capacity = lane_capacity or {Lane.INTERACTIVE: 2, Lane.BACKGROUND: 1}
        self.max_concurrent = max_concurrent
        self.lease_seconds = lease_seconds
        name = gate_tenant or "main"
        self.tenant = name
        cache = ContextCache()
        cache.pin(TenantContext(name=name, key="local", store=store, executor=executor,
                                lane_capacity=self.capacity, max_concurrent=max_concurrent,
                                lease_seconds=lease_seconds))
        tenants = HostTenants(cache)
        tenants.serve(ServedTenant(name, "local", clock=getattr(store, "_clock", None)))
        self.runner = HostRunner(tenants, gate, poll_seconds=idle_poll_seconds, owner=owner)
        self.runner.local.register(name, getattr(store, "_clock", None))
        self.owner = self.runner.owner
        signal = getattr(store, "_wake", None)
        if hasattr(signal, "listener"):
            signal.listener = lambda: self.runner.hint(name)

    @property
    def active_count(self) -> int:
        return self.runner.active_count

    async def claim_once(self) -> int:
        """Fill free slots in every lane. Returns the number of runs started."""
        self.runner.local.mark(self.tenant)  # a direct call always looks
        return await self.runner.claim_once()

    async def run_forever(self, stop: asyncio.Event, shutdown_grace: float = 30.0) -> None:
        await self.runner.run(stop, shutdown_grace)
