"""Crontal integration.

Contributes, from the same config vars as the legacy wiring in `server.py`
(`CRONTAL_URL`, `CRONTAL_AGENT_TOKEN`, `HANDLER_WEBHOOK_SECRET`; all optional,
each piece turns on only when its vars are set):
- thread type `crontal` (a built-in `ThreadType` value; instructions stay in
  the settings thread defaults);
- `/ingest/crontal` (`web.ingest.crontal_ingest_router_for`), registered once on
  the host's app and served to a tenant whose webhook secret is set;
- `CrontalApiTools` (`crontal_api_*`) when URL and token are set;
- the curated `crontal.*` MCP tools, discovered while the context loads from the
  host's cached listing (`connector.py`), when URL and token are set;
- the Crontal notification sink (`sink.py`) when URL and secret are set;
- default memory kinds (inquiry, company, conversation; `memory_kinds.py`).
"""
from __future__ import annotations

from typing import Any

from ..base import (
    Integration,
    IntegrationConfig,
    IntegrationContext,
    ThreadTypeSpec,
)

CONFIG_VARS = ("CRONTAL_URL", "CRONTAL_AGENT_TOKEN", "HANDLER_WEBHOOK_SECRET")


class CrontalIntegration(Integration):
    name = "crontal"
    label = "Crontal"

    def configure(self, config: IntegrationConfig) -> None:
        super().configure(config)
        env = config.env
        self.crontal_url = (env.get("CRONTAL_URL") or "").strip() or None
        self.agent_token = env.get("CRONTAL_AGENT_TOKEN") or None
        self.webhook_secret = env.get("HANDLER_WEBHOOK_SECRET") or None

    def settings_defaults(self) -> dict[str, dict]:
        from .memory_kinds import CRONTAL_MEMORY_DEFAULTS

        return {"memory": CRONTAL_MEMORY_DEFAULTS}

    def thread_types(self) -> list[ThreadTypeSpec]:
        return [ThreadTypeSpec(name="crontal", lane="background", visibility="shared",
                               acts_as="instance", label="Crontal")]

    def routers(self, tenant) -> list[Any]:
        from ...web.ingest import crontal_ingest_router_for

        def binding():
            ctx = tenant.context()
            return (ctx.store, tenant.integration().webhook_secret,
                    ctx.settings.harness_source("crontal"))

        return [crontal_ingest_router_for(binding)]

    def serves_routes(self, ctx: IntegrationContext) -> bool:
        return bool(self.webhook_secret)

    def tool_sets(self, ctx: IntegrationContext) -> list[Any]:
        if not (self.crontal_url and self.agent_token):
            return []
        from ...gateway.crontal_api import CrontalApiTools, crontal_base_url
        from ...run import thread_slug

        workspaces_dir = ctx.workspaces_dir
        return [CrontalApiTools(crontal_base_url(self.crontal_url), self.agent_token,
                                workspace_for=lambda key: workspaces_dir / thread_slug(key),
                                upload_sources=ctx.extra.setdefault("upload_sources", []))]

    def sinks(self, ctx: IntegrationContext) -> list[Any]:
        from .sink import crontal_sink_from_config

        sink = crontal_sink_from_config(self.crontal_url, self.agent_token,
                                        self.webhook_secret, ctx.instance_name)
        return [sink] if sink is not None else []

    async def load(self, ctx: IntegrationContext) -> bool:
        if not self.crontal_url:
            return True
        tools = ctx.extra.get("crontal_tools")
        if tools is None:
            tools = ctx.extra["crontal_tools"] = crontal_tools(ctx, self.crontal_url,
                                                               self.agent_token)
        return await tools.ensure()


def crontal_tools(ctx: IntegrationContext, url: str | None, token: str | None):
    """The tenant's curated Crontal tools, from the host's cached discovery."""
    from .connector import CrontalTools, default_discovery

    services = ctx.services
    discovery = getattr(services, "crontal_discovery", None) or default_discovery()
    return CrontalTools(discovery, url, token, ctx.gateway, ctx.instance_name or "main",
                        on_registered=ctx.extra.get("on_tools_changed"))
