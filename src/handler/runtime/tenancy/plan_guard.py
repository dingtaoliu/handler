"""What a plan without the admin console can reach, whoever is calling.

Crontal calls Handler with the instance admin key on behalf of merchant owners, so
checking for an admin role would not do it: the routes themselves are limited, to
what the chat panel needs. Anything not listed is refused, including new routes.
"""

from __future__ import annotations

import re

from starlette._utils import get_route_path
from starlette.responses import JSONResponse

from typing import Any

from .limits import LiveLimits, PlanLimits, upgrade_required

_PANEL_ROUTES = re.compile(
    r"^/(healthz|mcp(/.*)?|ingest/.*|api/("
    r"chat(/options)?|limits/inputs|threads(/.*)?|runs/.*|tasks(/.*)?|approvals(/.*)?"
    r"|questions(/.*)?|notifications(/.*)?|uploads(/.*)?|me|users(/[^/]+)?"
    r"|mailboxes(/.*)?|connections/google(/.*)?))$")


def allowed_without_console(path: str) -> bool:
    return _PANEL_ROUTES.fullmatch(path) is not None


def plan_refusal(path: str, limits: PlanLimits) -> dict[str, Any] | None:
    """The 403 body if this plan doesn't include this route, else None."""
    if not limits.automations and (path == "/api/automations"
                                   or path.startswith("/api/automations/")):
        return upgrade_required("automations", limits)
    if not limits.console and not allowed_without_console(path):
        return upgrade_required("console", limits)
    return None


class ConsoleGuard:
    def __init__(self, app, limits: LiveLimits) -> None:
        self.app, self.limits = app, limits

    async def __call__(self, scope, receive, send) -> None:
        body = (plan_refusal(get_route_path(scope), self.limits.current)
                if scope["type"] == "http" else None)
        if body is not None:
            await JSONResponse(body, 403)(scope, receive, send)
            return
        await self.app(scope, receive, send)
