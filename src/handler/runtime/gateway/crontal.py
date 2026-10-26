"""Crontal connector: credentials and the curated Crontal MCP tools.

The agent acts as the instance's Crontal AI user (`crontal_identity.py`), never
as a person: every call carries the agent token plus `X-Crontal-User: <AI user>`,
and Crontal enforces that member's permissions. On chat threads the chatting
person is added as `X-Crontal-On-Behalf-Of`, for Crontal's audit only.
"""

from __future__ import annotations

from typing import Any

from ..context import RunContext
from .credentials import UpstreamAccess
from .crontal_identity import (
    ON_BEHALF_OF_HEADER,
    CrontalIdentityClient,
    IdentityUnavailable,
    crontal_identity,
    on_behalf_of,
)
from .upstream import UpstreamConnector

# Owner strings for the upstream connector: the AI user, optionally noting
# the person a chat run works for ("crontal-ai" or "crontal-ai:obo:17").
AI_OWNER = "crontal-ai"
_OBO = ":obo:"

def _status_outward(args: dict[str, Any]) -> str | None:
    """Any status but draft makes the RFQ visible to suppliers (or notifies them)."""
    for source in (args, *(v for v in args.values() if isinstance(v, dict))):
        status = source.get("status")
        if status is not None and str(status).strip().lower() != "draft":
            return f"sets an RFQ's status to {status!r} (visible to suppliers)"
    return None


# Crontal tool name → handler curation. Reads are allowed by default; anything
# that creates or changes Crontal data needs approval (write_external).
# `outward` marks calls that leave the merchant (see ToolSpec.outward).
CRONTAL_TOOLS: dict[str, dict[str, Any]] = {
    "app_get_rfqs": {"risk": "read", "rename": "get_rfqs"},
    "app_get_rfq": {"risk": "read", "rename": "get_rfq"},
    "app_get_bids": {"risk": "read", "rename": "get_bids"},
    "app_get_rfq_bids": {"risk": "read", "rename": "get_rfq_bids"},
    "app_get_demand_listing": {"risk": "read", "rename": "get_demand_listing"},
    "app_get_shape_definitions": {"risk": "read", "rename": "get_shape_definitions"},
    "app_create_rfq": {"risk": "write_external", "rename": "create_rfq",
                       "outward": _status_outward},
    "app_update_rfq": {"risk": "write_external", "rename": "update_rfq",
                       "outward": _status_outward},
    "app_publish_rfq": {"risk": "write_external", "rename": "publish_rfq",
                        "outward": lambda args: "publishes an RFQ to suppliers"},
    "app_add_demand_listings": {"risk": "write_external", "rename": "add_demand_listings",
                                "outward": lambda args: None},
    "app_delete_demand_listings": {"risk": "write_external",
                                   "rename": "delete_demand_listings",
                                   "outward": lambda args: None},
    # Shapes are a shared catalogue: no `outward`, so it always counts as outward.
    "app_define_shape": {"risk": "write_external", "rename": "define_shape"},
}


class CrontalCredentials:
    """Agent token + the instance's Crontal AI user, on every thread type."""

    def __init__(self, url: str, agent_token: str,
                 identity: CrontalIdentityClient | None = None) -> None:
        self.url = url
        self._token = agent_token
        from .crontal_api import crontal_base_url

        self.identity = identity or crontal_identity(crontal_base_url(url), agent_token)

    async def resolve(self, connector: str, owner: str) -> UpstreamAccess | None:
        if owner != AI_OWNER and not owner.startswith(AI_OWNER + _OBO):
            return None
        try:
            identity = await self.identity.get()
        except IdentityUnavailable as exc:
            raise PermissionError(f"No Crontal AI identity for this instance: {exc}") from exc
        headers = {"Authorization": f"Bearer {self._token}",
                   "X-Crontal-User": identity.user_id}
        obo = owner.partition(_OBO)[2]
        if obo:
            headers[ON_BEHALF_OF_HEADER] = obo
        return UpstreamAccess(self.url, headers)

    async def discovery_access(self, connector: str) -> UpstreamAccess | None:
        # Crontal allows initialize/tools/list with the agent token alone.
        return UpstreamAccess(self.url, {"Authorization": f"Bearer {self._token}"})


def crontal_owner(ctx: RunContext) -> str:
    obo = on_behalf_of(ctx)
    return f"{AI_OWNER}{_OBO}{obo}" if obo else AI_OWNER


def crontal_connector(url: str, agent_token: str) -> UpstreamConnector:
    return UpstreamConnector("crontal", CrontalCredentials(url, agent_token),
                             CRONTAL_TOOLS, owner_for=crontal_owner)
