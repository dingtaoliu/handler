"""The host's one HTTP app (role-split-design.md, step 3b: "one routed app").

Every router is registered once, here, with `TenantLocal` proxies in place of a
tenant's objects (tenancy/routing.py). The front (tenancy/host.py) resolves each
request's tenant from its credentials and serves it with that tenant's context in
a context variable; a handler then reads the store, bus, gateway and the rest of
that tenant's objects from the context's `parts`.

Routes only some tenants have are put in route groups: `email_ingest` (a tenant
with an email secret) and one group per integration (a tenant that enables it).
`TenantGuard` refuses the others with a 404, and applies the plan's refusals.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from ..gateway.api import approvals_router, questions_router
from ..integrations.registry import IntegrationRegistry, host_routers, route_group, route_keys
from ..tenancy.routing import (
    RouteGroups,
    TenantAuth,
    TenantGuard,
    current_context,
    instance_label,
    part,
    value,
)
from .admin_api import admin_router
from .app import create_app
from .automation_dashboard import automation_dashboard_router
from .email_ingest import email_ingest_router_for
from .google_api import google_router_for
from .memory_recovery_api import memory_recovery_router
from .settings_api import settings_router
from .tasks_api import tasks_router
from .ui_manifest_api import ui_manifest_router
from .usage_api import usage_router

EMAIL_INGEST = "email_ingest"


def _harness(thread_type: str):
    async def lookup() -> str:
        return await current_context().parts.settings.harness_source(thread_type)()

    return lookup


def _added(app: FastAPI, router: Any) -> list[Any]:
    """Include the router; returns the app routes it added."""
    before = len(app.router.routes)
    app.include_router(router)
    return app.router.routes[before:]


def create_routed_app(mcp: Any, integrations: IntegrationRegistry) -> FastAPI:
    """Build the host's app once. `mcp` is the host's one MCP app (also reachable
    at `/mcp` under a tenant's prefix); `integrations` are every integration a
    tenant on this host may enable."""
    auth = TenantAuth()
    store, bus, gateway = part("store"), part("bus"), part("gateway")

    def wake_cancel(run_id: str) -> None:
        current_context().parts.executor.wake_cancel(run_id)

    app = create_app(store, bus, auth, default_harness=_harness("chat"),
                     uploads=part("uploads"), instance_name=instance_label,
                     cancel_wake=wake_cancel)
    app.mount("/mcp", mcp)
    app.include_router(approvals_router(gateway, store, auth))
    app.include_router(memory_recovery_router(gateway, store, auth))
    app.include_router(questions_router(part("questions"), store, auth))
    app.include_router(tasks_router(store, bus, auth))
    app.include_router(usage_router(store, auth))
    app.include_router(automation_dashboard_router(store, auth, part("settings")))

    def crontal_clients():
        parts = current_context().parts
        return parts.crontal_identity, parts.crontal_catalog

    async def tools_for(ctx):
        return await current_context().parts.effective_tool_names(ctx)

    app.include_router(settings_router(
        part("settings"), auth, part("memory_tools"), crontal=crontal_clients,
        available_tools=lambda: current_context().parts.available_tools(),
        available_tools_for=tools_for))
    app.include_router(admin_router(
        store, auth, scheduler=part("scheduler"), gateway=gateway, uploads=part("uploads"),
        log_file=value("log_file"), artifact_store=part("artifact_store"),
        outbox=part("outbox")))
    app.include_router(google_router_for(value("google"), auth))
    app.include_router(ui_manifest_router(part("integrations"), auth))

    groups = RouteGroups()
    groups.add(EMAIL_INGEST, _added(app, email_ingest_router_for(value(EMAIL_INGEST))))
    for name, routers in host_routers(integrations, auth,
                                      reserved_routes=set(route_keys(app))):
        for router in routers:
            groups.add(route_group(name), _added(app, router))
    app.add_middleware(TenantGuard, groups=groups)
    app.state.route_groups = groups
    return app
