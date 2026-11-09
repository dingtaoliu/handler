"""Wake on demand: one runtime per tenant, existing only while it has a reason to.

The TenantHost is the ASGI app in front of every tenant and the owner of each
tenant's lifecycle. All starts and stops of one tenant run under that tenant's
lock, so there is never more than one runtime for it, and a request or
notification that arrives during a stop waits and then starts a fresh one. Locks
are per tenant, so a slow stop holds up no other tenant.

Work on an inactive tenant is found by any of: its HTTP request; a notification for
its schema; `tenant_state.next_wake_at`, which a process outside this host writes
in the same transaction as the work and a stop writes from the tenant's schedules
and retries; and a sweep of each tenant's own tables at boot and once more after
the boot grace, for writes that predate any of these.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from psycopg.errors import UndefinedTable
from starlette._utils import get_route_path
from starlette.responses import JSONResponse

from .registry import Tenant, TenantRegistry

logger = logging.getLogger("handler.runtime.tenancy")

ROOT_TENANT = "main"
# Integrations that poll for input, so their tenant can never sleep.
PINNED_INTEGRATIONS = frozenset({"mailbox", "gmail", "google"})
_INSTANCE_PATH = re.compile(r"^/i/([a-z][a-z0-9_]{0,31})(/.*)$")
ACTIVATION_RETRY_SECONDS = 30.0


@dataclass
class _Runtime:
    app: Any
    stop: asyncio.Event
    owner: asyncio.Task[None]


@dataclass
class _Slot:
    tenant: Tenant
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    runtime: _Runtime | None = None
    activation: asyncio.Task[Any] | None = None
    deactivation: asyncio.Task[Any] | None = None
    in_flight: set[asyncio.Task[Any]] = field(default_factory=set)
    last_activity: float = field(default_factory=monotonic)
    stale: bool = False  # its config changed; restart once quiescent
    recover: bool = False  # a stop could not record its state

    @property
    def pinned(self) -> bool:
        return bool(PINNED_INTEGRATIONS.intersection(self.tenant.integrations or ()))

    def touch(self) -> None:
        self.last_activity = monotonic()


RuntimeFactory = Callable[[Tenant], Any]  # an async context manager yielding the app


class TenantHost:
    def __init__(self, registry: TenantRegistry, runtime_for: RuntimeFactory, *,
                 idle_seconds: float = 600.0, scan_seconds: float = 15.0,
                 boot_grace_seconds: float = 120.0, concurrent_activations: int = 4) -> None:
        self.registry = registry
        self._runtime_for = runtime_for
        self.idle_seconds = idle_seconds
        self.scan_seconds = scan_seconds
        self.boot_grace_seconds = boot_grace_seconds
        self._starting = asyncio.Semaphore(concurrent_activations)
        self._slots: dict[str, _Slot] = {}
        self._by_schema: dict[str, _Slot] = {}
        self._scan_now = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    # --- ASGI ---------------------------------------------------------------
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return
        name, child = self._route(scope)
        slot = await self._slot(name) if name else None
        if slot is None or not slot.tenant.enabled:
            await self._refuse(scope, receive, send, 404, "instance not found")
            return
        task = asyncio.current_task()
        slot.in_flight.add(task)  # before any await: a stop must see this request
        try:
            try:
                runtime = await self._runtime(slot)
            except Exception:
                logger.exception("tenant activation failed", extra={"tenant": slot.tenant.name})
                await self._refuse(scope, receive, send, 503, "instance unavailable",
                                   headers={"Retry-After": "5"})
                return
            if runtime is None:
                # Shutting down must read as "retry", so senders don't give up on mail.
                status, detail = ((503, "instance unavailable") if self._closing
                                  else (404, "instance not found"))
                await self._refuse(scope, receive, send, status, detail)
                return
            slot.touch()
            await runtime.app(child, receive, send)
        finally:
            slot.in_flight.discard(task)
            slot.touch()

    @staticmethod
    def _route(scope) -> tuple[str | None, dict]:
        path = get_route_path(scope)
        match = _INSTANCE_PATH.match(path)
        if match:
            root = scope.get("root_path", "")
            return match.group(1), {**scope, "root_path": f"{root}/i/{match.group(1)}",
                                    "app_root_path": scope.get("app_root_path", root)}
        if path == "/i" or path.startswith("/i/"):
            return None, scope
        return ROOT_TENANT, scope

    @staticmethod
    async def _refuse(scope, receive, send, status: int, detail: str, headers=None) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013 if status == 503 else 1008})
            return
        await JSONResponse({"detail": detail}, status, headers)(scope, receive, send)

    async def _slot(self, name: str) -> _Slot | None:
        slot = self._slots.get(name)
        if slot is not None:
            return slot
        tenant = await self.registry.get(name)
        return self._remember(tenant) if tenant else None

    def _remember(self, tenant: Tenant) -> _Slot:
        slot = self._slots.get(tenant.name)
        if slot is None:
            slot = self._slots[tenant.name] = _Slot(tenant)
        slot.tenant = tenant
        self._by_schema[tenant.schema_name] = slot
        return slot

    # --- lifecycle: every transition holds the tenant's lock ----------------
    async def _runtime(self, slot: _Slot) -> _Runtime | None:
        if slot.runtime is not None and not slot.lock.locked():
            return slot.runtime  # no await before the caller counts as in flight
        return await asyncio.shield(self._activation(slot))

    def _activation(self, slot: _Slot) -> asyncio.Task[Any]:
        """The tenant's one activation; shared, so a caller leaving can't abandon a start."""
        if slot.activation is None or slot.activation.done():
            slot.activation = asyncio.create_task(self._activate(slot),
                                                  name=f"activate-{slot.tenant.name}")
            slot.activation.add_done_callback(self._log_failure)
        return slot.activation

    @staticmethod
    def _log_failure(task: asyncio.Task[Any]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("tenant lifecycle step failed", exc_info=task.exception())

    async def _activate(self, slot: _Slot) -> _Runtime | None:
        async with slot.lock:
            if slot.runtime is not None or not slot.tenant.enabled or self._closing:
                return slot.runtime
            name = slot.tenant.name
            async with self._starting:
                # Before the start: a crash from here on is recovered at boot.
                await self.registry.mark_active(name)
                try:
                    slot.runtime = await self._start(slot)
                except BaseException:
                    with suppress(Exception):
                        await self.registry.activation_failed(name, ACTIVATION_RETRY_SECONDS)
                    raise
            slot.recover = False
            slot.touch()
            logger.info("tenant activated", extra={"tenant": name})
            return slot.runtime

    async def _start(self, slot: _Slot) -> _Runtime:
        tenant = await self.registry.get(slot.tenant.name) or slot.tenant
        slot.tenant = tenant
        ready = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()

        async def own_runtime() -> None:
            # MCP uses AnyIO cancel scopes: enter AND exit the runtime in this task.
            try:
                async with self._runtime_for(tenant) as app:
                    ready.set_result(app)
                    await stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    raise

        owner = asyncio.create_task(own_runtime(), name=f"tenant-{tenant.name}")
        owner.add_done_callback(self._log_failure)
        return _Runtime(await ready, stop, owner)

    async def _quiescent(self, slot: _Slot, runtime: _Runtime) -> bool:
        """No in-flight run, open request or SSE stream, due work or unflushed files."""
        parts = runtime.app.state.lifecycle
        if slot.in_flight or parts.dispatcher.active_count:
            slot.touch()
            return False
        due = await parts.store.earliest_work(list(parts.outbox.sinks))
        if due is not None and due <= parts.store._clock():
            return False
        if parts.workspace_sync.dirty:
            await parts.workspace_sync.flush()
            if parts.workspace_sync.dirty:
                logger.warning("tenant stays active: workspace flush failed",
                               extra={"tenant": slot.tenant.name})
                return False
        return not slot.in_flight

    async def _deactivate(self, slot: _Slot, *, force: bool = False) -> bool:
        async with slot.lock:
            runtime = slot.runtime
            if runtime is None:
                return False
            if force:
                for task in list(slot.in_flight):
                    task.cancel()
            elif not await self._quiescent(slot, runtime):
                return False
            name, schema = slot.tenant.name, slot.tenant.schema_name
            sinks = list(runtime.app.state.lifecycle.outbox.sinks)
            runtime.stop.set()
            await asyncio.gather(runtime.owner, return_exceptions=True)
            slot.runtime, slot.stale = None, False
            try:
                # The wake goes first: if we die before active_since clears, the
                # next boot still activates this tenant.
                await self.registry.write_next_wake(name, schema, sinks)
                await self.registry.clear_active(name)
            except Exception:
                slot.recover = True
                logger.exception("tenant stop could not be recorded", extra={"tenant": name})
                return False
            logger.info("tenant deactivated", extra={"tenant": name})
            return True

    def _start_deactivation(self, slot: _Slot) -> None:
        if slot.deactivation is None or slot.deactivation.done():
            slot.deactivation = asyncio.create_task(self._deactivate(slot),
                                                    name=f"deactivate-{slot.tenant.name}")
            slot.deactivation.add_done_callback(self._log_failure)

    # --- platform API hooks -------------------------------------------------
    async def tenant_changed(self, name: str) -> None:
        tenant = await self.registry.get(name)
        if tenant is None:
            return
        was_running = name in self._slots and self._slots[name].runtime is not None
        slot = self._remember(tenant)
        if not tenant.enabled:
            await self._deactivate(slot, force=True)
        elif was_running:
            slot.stale = True  # new config and secrets take effect on the next start

    # --- the one scanning task ----------------------------------------------
    async def run(self) -> None:
        await self.boot()
        resweep_at = monotonic() + self.boot_grace_seconds
        while not self._closing:
            try:
                await self.tick()
                if resweep_at is not None and monotonic() >= resweep_at:
                    resweep_at = None
                    await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tenant scan failed")
            self._scan_now.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._scan_now.wait(), self.scan_seconds)

    async def boot(self) -> None:
        tenants = await self.registry.list()
        for tenant in tenants:
            slot = self._remember(tenant)
            if tenant.enabled and (tenant.active_since is not None or slot.pinned):
                self._activation(slot)
        self.registry.hub.on_unattached = self._notified
        self.registry.hub.on_listening = self._scan_now.set
        await self.sweep()

    async def sweep(self) -> None:
        """Look inside each inactive tenant once, for work no signal told us about."""
        for slot in list(self._slots.values()):
            if slot.runtime is not None or not slot.tenant.enabled:
                continue
            try:
                if await self.registry.sweep_tenant(slot.tenant.name, slot.tenant.schema_name):
                    self._activation(slot)
            except UndefinedTable:
                pass  # created but never started: nothing in it yet
            except Exception:
                logger.exception("tenant sweep failed", extra={"tenant": slot.tenant.name})

    def _notified(self, schema: str) -> None:
        slot = self._by_schema.get(schema)
        if slot is not None and slot.tenant.enabled and not self._closing:
            self._activation(slot)

    async def tick(self) -> None:
        for name, seen in await self.registry.due():
            slot = await self._slot(name)
            if slot is None:
                continue
            if slot.runtime is not None and not slot.lock.locked():
                await self.registry.clear_wake(name, seen)  # its dispatcher polls for itself
                slot.runtime.app.state.lifecycle.store._wake.set()
            else:
                self._activation(slot)
        for slot in self._slots.values():
            if not slot.tenant.enabled:
                continue
            if slot.runtime is None:
                if slot.pinned or slot.recover:
                    self._activation(slot)
            elif slot.stale or (not slot.pinned
                                and monotonic() - slot.last_activity >= self.idle_seconds):
                self._start_deactivation(slot)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="tenant-scan")
        self._task.add_done_callback(self._log_failure)

    async def shutdown(self) -> None:
        """Stop every runtime together. active_since stays set, so a restart resumes them."""
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        slots = list(self._slots.values())
        pending = [t for s in slots for t in (s.activation, s.deactivation) if t is not None]
        await asyncio.gather(*pending, return_exceptions=True)
        for slot in slots:
            if slot.runtime is not None:
                slot.runtime.stop.set()
        results = await asyncio.gather(
            *(s.runtime.owner for s in slots if s.runtime is not None), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.error("tenant lifecycle failed", exc_info=result)
