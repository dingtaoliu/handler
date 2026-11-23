"""The request side of a tenant (role-split-design.md, step 3b): one routed app.

The host has one FastAPI app. Its routers are registered once; each request runs
with its tenant's context in a context variable (`serving`), and route handlers
reach that tenant's objects through `TenantLocal` proxies, which look the object up
on every use. Outside a served request a proxy fails instead of guessing a tenant.

Which tenant a request is for is decided in front of the app, from its credentials
(tenancy/host.py); the app never chooses one. Routes a tenant doesn't have (an
integration it hasn't enabled, email intake without a secret) are refused here by
`RouteGroups`, so one app serves tenants with different integrations.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import Any

from fastapi import HTTPException, Request
from starlette._utils import get_route_path
from starlette.responses import JSONResponse
from starlette.routing import Match

from ..context import current_tenant, reset_current_tenant, set_current_tenant
from .limits import upgrade_required
from .plan_guard import plan_refusal

_context: ContextVar[Any] = ContextVar("handler_tenant_context", default=None)


class NoTenantContext(RuntimeError):
    """A tenant's object was used outside a request served for a tenant."""


def current_context() -> Any:
    """The TenantContext of the request being served; raises outside one."""
    context = _context.get()
    if context is None:
        raise NoTenantContext("no tenant context for this request")
    return context


def context_or_none() -> Any:
    return _context.get()


@contextlib.contextmanager
def serving(context: Any) -> Iterator[Any]:
    """Run the block as `context`'s tenant (context variable and tenant name)."""
    token = _context.set(context)
    tenant_token = set_current_tenant(context.name)
    try:
        yield context
    finally:
        reset_current_tenant(tenant_token)
        _context.reset(token)


def instance_label() -> str:
    """The instance name pages and sign-in tickets use: the served context's, or the
    tenant a public page is answered for."""
    context = _context.get()
    if context is not None:
        return context.instance_name
    return current_tenant() or ""


class TenantLocal:
    """Stands in for one tenant object in a router built once for the host: every
    attribute is read from the object of the request's tenant."""

    __slots__ = ("_resolve", "_label")

    def __init__(self, resolve: Callable[[], Any], label: str = "") -> None:
        object.__setattr__(self, "_resolve", resolve)
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"tenant object {self._label!r} is read-only here")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._resolve()(*args, **kwargs)

    def __repr__(self) -> str:
        return f"<TenantLocal {self._label}>"


def part(name: str) -> TenantLocal:
    """The request tenant's object `name` (its context's `parts`)."""
    return TenantLocal(lambda: getattr(current_context().parts, name), name)


def value(name: str) -> Callable[[], Any]:
    """A zero-argument getter of the request tenant's value `name`."""
    return lambda: getattr(current_context().parts, name)


class TenantAuth:
    """The request tenant's Authenticator, as FastAPI dependencies (registered once)."""

    users = TenantLocal(lambda: current_context().parts.auth.users, "users")

    async def require_principal(self, request: Request):
        return await current_context().parts.auth.require_principal(request)

    async def require_sse_principal(self, request: Request):
        return await current_context().parts.auth.require_sse_principal(request)

    async def require_admin(self, request: Request):
        return await current_context().parts.auth.require_admin(request)

    async def authenticate(self, credential: str, act_as: str | None):
        return await current_context().parts.auth.authenticate(credential, act_as)


class TenantRoutes:
    """What an integration's routers get (Integration.routers): the request tenant's
    own configured integration and its IntegrationContext. Its routes are served only
    to tenants that enable the integration, so these are always that tenant's."""

    def __init__(self, integration: str, auth: TenantAuth) -> None:
        self.name = integration
        self.group = f"integration:{integration}"
        self.auth = auth

    def _binding(self) -> tuple[Any, Any]:
        binding = current_context().routes.get(self.group)
        if binding is None:  # the route groups refuse first; this is a backstop
            raise HTTPException(404, "Not Found")
        return binding

    def integration(self) -> Any:
        """This tenant's configured instance of the integration."""
        return self._binding()[0]

    def context(self) -> Any:
        """This tenant's IntegrationContext (store, settings, gateway, ...)."""
        return self._binding()[1]

    def local(self, resolve: Callable[[Any, Any], Any], label: str = "") -> TenantLocal:
        """A proxy for `resolve(integration, context)` of the request's tenant."""
        return TenantLocal(lambda: resolve(*self._binding()), label or self.name)


class RouteGroups:
    """Routes only some tenants have: a group name per route, served to a tenant only
    when its context binds that group (`TenantContext.routes`)."""

    def __init__(self) -> None:
        self._routes: list[tuple[Any, str]] = []

    def add(self, group: str, routes: list[Any]) -> None:
        self._routes.extend((route, group) for route in routes)

    def group_of(self, scope) -> str | None:
        for route, group in self._routes:
            match, _ = route.matches(scope)
            if match is not Match.NONE:
                return group
        return None

    def groups(self) -> set[str]:
        return {group for _, group in self._routes}


PUBLIC_PATHS = ("/", "/app", "/app/", "/login", "/healthz")


def is_public_page(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/app/static/")


class TenantGuard:
    """In front of the one app's routes: the plan's refusals and the tenant's route
    groups (both used to be per-tenant app wiring). A request served with no tenant
    context may reach only the public pages."""

    def __init__(self, app, groups: RouteGroups) -> None:
        self.app, self.groups = app, groups

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = get_route_path(scope)
        context = _context.get()
        if context is None:
            if not is_public_page(path):
                await JSONResponse({"detail": "Not Found"}, 404)(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        limits = context.limits.current if context.limits is not None else None
        if limits is not None and scope["type"] == "http":
            body = plan_refusal(path, limits)
            if (body is None and not limits.connected_mail and scope["method"] == "POST"
                    and path == "/api/mailboxes"):
                body = upgrade_required("mailbox", limits)
            if body is not None:
                await JSONResponse(body, 403)(scope, receive, send)
                return
        group = self.groups.group_of(scope)
        if group is not None and group not in context.routes:
            await JSONResponse({"detail": "Not Found"}, 404)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class TenantApp:
    """One tenant's view of the host's app: serves a request as that tenant.

    Not an app of its own (it holds no routes): the front calls it once it has
    resolved the request's tenant, and tests and the spool replay call it directly.
    `state` carries the tenant's objects for tests (lifecycle, gateway, outbox, ...).
    """

    def __init__(self, app: Any, context: Any, state: Any) -> None:
        self.app, self.context, self.state = app, context, state

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            return  # the host owns the lifespan
        with serving(self.context):
            await self.app(scope, receive, send)
