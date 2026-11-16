"""Per-run context, carried in a ContextVar so concurrent runs never share state."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RunContext:
    run_id: str
    thread_key: str
    user_id: str | None = None  # None for system-triggered runs
    acting_as: str | None = None  # user whose authority the agent is exercising
    parent_run_id: str | None = None


_current: ContextVar[RunContext | None] = ContextVar("handler_run_context", default=None)


def current_run() -> RunContext | None:
    return _current.get()


def set_current_run(ctx: RunContext | None):
    """Set the run context; returns a token for ContextVar.reset()."""
    return _current.set(ctx)


def reset_current_run(token) -> None:
    _current.reset(token)


# The tenant (instance) whose work is running: set for each instance's
# background tasks (runs included) and HTTP requests, so shared pools such as
# the reader pool can share fairly. None outside any instance.
_tenant: ContextVar[str | None] = ContextVar("handler_tenant", default=None)


def current_tenant() -> str | None:
    return _tenant.get()


def set_current_tenant(name: str | None):
    """Set the tenant; returns a token for reset_current_tenant()."""
    return _tenant.set(name)


def reset_current_tenant(token) -> None:
    _tenant.reset(token)


class TenantScope:
    """ASGI middleware: every request to an instance's app runs as its tenant."""

    def __init__(self, app, tenant: str) -> None:
        self.app, self.tenant = app, tenant

    async def __call__(self, scope, receive, send) -> None:
        token = _tenant.set(self.tenant)
        try:
            await self.app(scope, receive, send)
        finally:
            _tenant.reset(token)
