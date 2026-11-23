"""A tenant is context (role-split-design.md, step 3): built on demand, cached, dropped.

`TenantContext` is everything a run, a timer firing, a delivery or a request needs for
one tenant: its store on the shared hub, settings and grant policy, live plan limits,
gateway, executor, sinks, timer handlers, workspace and file store, integration
context, and the route groups of the host's one app it is served. Building one starts no long-lived task and
opens no connection of its own on a hub (the store borrows the hub's pool).

`ContextCache` keeps built contexts in an LRU with a TTL. A context in use (a run, a
firing, a delivery pass, an open request) is pinned and never closed under its user.
`invalidate` (from `NOTIFY handler_tenant_changed`, sent on any registry, plan or
settings change by any process) retires the entry: the next user gets a fresh build,
and the old one closes when its last user leaves. An idle tenant costs at most its
cache entry; an evicted one costs nothing.

`HostTenants` is the host's directory: which tenants it serves (name, due key,
whether its rows are in a hub's control schema, enabled, plan). It answers every
"which tenants" question without a tenant-scoped query.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

logger = logging.getLogger("handler.runtime.tenancy")

DEFAULT_CACHE_SIZE = 32
DEFAULT_CACHE_TTL_SECONDS = 600.0


@dataclass
class TenantContext:
    """One tenant's objects, bound together. Holds no task between uses."""

    name: str
    key: str  # its due-work and tenant_state key: the schema name, or "local" (SQLite)
    store: Any
    app: Any = None  # its view of the host's one app (tenancy/routing.py TenantApp)
    executor: Any = None
    gateway: Any = None
    outbox: Any = None  # the tenant's sinks (the host outbox loop delivers for it)
    settings: Any = None
    limits: Any = None  # LiveLimits on registry tenants, else None
    timer_target: Any = None
    workspace_sync: Any = None
    lifecycle: Any = None  # the namespace timer handlers and tests reach objects through
    lane_capacity: dict = field(default_factory=dict)
    max_concurrent: int | None = None
    lease_seconds: float = 120.0
    close: Callable[[], Awaitable[None]] | None = None
    instance_name: str = ""  # as configured: "" for a lone instance, else its name
    routes: dict = field(default_factory=dict)  # route group -> what its handlers use

    @property
    def parts(self) -> Any:
        """The tenant's objects the host app's handlers read (web/host_app.py)."""
        return self.lifecycle


@dataclass
class _Entry:
    name: str
    generation: int
    context: TenantContext
    pinned: bool = False
    users: int = 0
    last_used: float = 0.0
    retired: bool = False


Builder = Callable[[str], Awaitable[TenantContext]]


class ContextCache:
    def __init__(self, build: Builder | None = None, *, max_size: int = DEFAULT_CACHE_SIZE,
                 ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
                 clock: Callable[[], float] = monotonic) -> None:
        self.build = build
        self.max_size = max(1, max_size)
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._generations: dict[str, int] = {}
        self._building: dict[str, asyncio.Task[_Entry]] = {}
        self._closing: set[asyncio.Task[None]] = set()
        self._closed = False
        self.builds = 0  # for tests and logs

    # --- reading -------------------------------------------------------------
    def cached(self, name: str) -> TenantContext | None:
        """The current context if one is built (never builds)."""
        entry = self._entries.get(name)
        return entry.context if entry is not None else None

    def names(self) -> list[str]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    @contextlib.asynccontextmanager
    async def use(self, name: str) -> AsyncIterator[TenantContext]:
        """The tenant's context, pinned for the duration (built if needed)."""
        entry = await self._acquire(name)
        try:
            yield entry.context
        finally:
            self._release(entry)

    async def _acquire(self, name: str) -> _Entry:
        if self._closed:
            raise RuntimeError("the tenant context cache is closed")
        self.expire()
        entry = self._entries.get(name)
        if entry is not None and entry.generation == self._generations.get(name, 0):
            entry.users += 1
            self._entries.move_to_end(name)
            return entry
        task = self._building.get(name)
        if task is None:
            task = asyncio.create_task(self._build(name), name=f"context-build:{name}")
            self._building[name] = task
            task.add_done_callback(lambda _t, name=name: self._building.pop(name, None))
        entry = await asyncio.shield(task)
        entry.users += 1
        return entry

    async def _build(self, name: str) -> _Entry:
        if self.build is None:
            raise LookupError(f"tenant {name!r} is not served here")
        generation = self._generations.get(name, 0)
        context = await self.build(name)
        self.builds += 1
        entry = _Entry(name, generation, context, last_used=self._clock())
        old = self._entries.pop(name, None)
        if old is not None:
            self._retire(old)
        if generation != self._generations.get(name, 0):
            entry.retired = True  # invalidated while building: serve this use, then close
        else:
            self._entries[name] = entry
        logger.info("tenant context built", extra={"tenant": name})
        return entry

    def _release(self, entry: _Entry) -> None:
        entry.users -= 1
        entry.last_used = self._clock()
        if entry.users <= 0 and entry.retired:
            self._close(entry)
            return
        self._trim()

    # --- writing ---------------------------------------------------------------
    def pin(self, context: TenantContext) -> None:
        """A context the host built itself and keeps for its lifetime (a lone instance,
        or a tenant on a store with its own connections, which only it can hear)."""
        entry = _Entry(context.name, self._generations.get(context.name, 0), context,
                       pinned=True, last_used=self._clock())
        self._entries[context.name] = entry

    def invalidate(self, name: str) -> None:
        """Its registry row, plan or settings changed: the next use builds afresh."""
        entry = self._entries.get(name)
        if entry is not None and entry.pinned:
            return  # rebuilt only by its owner; it reads settings live
        self._generations[name] = self._generations.get(name, 0) + 1
        entry = self._entries.pop(name, None)
        if entry is not None:
            self._retire(entry)

    def _retire(self, entry: _Entry) -> None:
        entry.retired = True
        if entry.users <= 0:
            self._close(entry)

    def expire(self) -> int:
        """Close unused contexts older than the TTL; returns how many (no queries)."""
        now = self._clock()
        stale = [entry for entry in self._entries.values()
                 if not entry.pinned and entry.users <= 0
                 and now - entry.last_used >= self.ttl_seconds]
        for entry in stale:
            del self._entries[entry.name]
            self._close(entry)
        return len(stale)

    def _trim(self) -> None:
        while len(self._entries) > self.max_size:
            victim = next((entry for entry in self._entries.values()
                           if not entry.pinned and entry.users <= 0), None)
            if victim is None:
                return  # everything is in use: over the size until something is released
            del self._entries[victim.name]
            self._close(victim)

    def _close(self, entry: _Entry) -> None:
        entry.retired = True
        close = entry.context.close
        if close is None:
            return

        async def run() -> None:
            try:
                await close()
            except Exception:
                logger.exception("tenant context close failed", extra={"tenant": entry.name})
            logger.info("tenant context closed", extra={"tenant": entry.name})

        task = asyncio.create_task(run(), name=f"context-close:{entry.name}")
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def settle(self) -> None:
        """Wait for contexts being closed (tests, shutdown)."""
        while self._closing:
            await asyncio.gather(*list(self._closing), return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        if self._building:
            await asyncio.gather(*list(self._building.values()), return_exceptions=True)
        for entry in list(self._entries.values()):
            del self._entries[entry.name]
            if entry.users <= 0:
                self._close(entry)
            else:
                entry.retired = True
        await self.settle()


@dataclass
class ServedTenant:
    """What the host knows about a tenant without a tenant-scoped query."""

    name: str
    key: str  # schema name on Postgres; "local" for a SQLite file
    hub: Any = None  # the PostgresHub whose control schema holds its rows, if any
    enabled: bool = True
    plan: str = "paid"
    clock: Callable[[], Any] | None = None  # a store's own clock (local tenants)


class HostTenants:
    """The host's tenants and their contexts: the one place a tenant is looked up."""

    def __init__(self, cache: ContextCache) -> None:
        self.cache = cache
        self._served: dict[str, ServedTenant] = {}
        self._by_key: dict[tuple[int, str], str] = {}
        self._admin_keys: dict[bytes, str] = {}  # sha256(admin key) -> tenant
        self.on_hint: Callable[[str], None] | None = None  # the host loops' wake

    # --- credentials that name a tenant (the front door, tenancy/host.py) ------
    def set_admin_key(self, name: str, key: str | None) -> None:
        """Remember which tenant an admin key belongs to (only its digest is kept)."""
        for digest in [d for d, owner in self._admin_keys.items() if owner == name]:
            del self._admin_keys[digest]
        if key:
            self._admin_keys[hashlib.sha256(key.encode()).digest()] = name

    def admin_key_owner(self, credential: str | None) -> str | None:
        """The tenant whose admin key this is, if it is one."""
        if not credential:
            return None
        return self._admin_keys.get(hashlib.sha256(credential.encode()).digest())

    def serve(self, tenant: ServedTenant) -> None:
        old = self._served.get(tenant.name)
        if old is not None:
            self._by_key.pop((id(old.hub), old.key), None)
        self._served[tenant.name] = tenant
        self._by_key[(id(tenant.hub), tenant.key)] = tenant.name

    def drop(self, name: str) -> None:
        self.set_admin_key(name, None)
        old = self._served.pop(name, None)
        if old is not None:
            self._by_key.pop((id(old.hub), old.key), None)
        self.cache.invalidate(name)

    def get(self, name: str) -> ServedTenant | None:
        return self._served.get(name)

    def all(self) -> list[ServedTenant]:
        return list(self._served.values())

    def enabled(self) -> list[ServedTenant]:
        return [tenant for tenant in self._served.values() if tenant.enabled]

    def by_key(self, hub: Any, key: str) -> ServedTenant | None:
        name = self._by_key.get((id(hub), key))
        return self._served.get(name) if name is not None else None

    def hubs(self) -> list[Any]:
        seen: dict[int, Any] = {}
        for tenant in self._served.values():
            if tenant.hub is not None:
                seen.setdefault(id(tenant.hub), tenant.hub)
        return list(seen.values())

    @contextlib.asynccontextmanager
    async def use(self, name: str) -> AsyncIterator[TenantContext | None]:
        """The context of an enabled tenant, pinned while in use; None if not served."""
        tenant = self._served.get(name)
        if tenant is None or not tenant.enabled:
            yield None
            return
        async with self.cache.use(name) as context:
            yield context

    def changed(self, hub: Any, schema: str) -> None:
        """NOTIFY handler_tenant_changed: drop the tenant's context."""
        tenant = self.by_key(hub, schema)
        if tenant is not None:
            self.cache.invalidate(tenant.name)
