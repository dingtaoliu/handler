"""Crontal integration.

Contributes, from the same config vars as the legacy wiring in `server.py`
(`CRONTAL_URL`, `CRONTAL_AGENT_TOKEN`, `HANDLER_WEBHOOK_SECRET`; all optional,
each piece turns on only when its vars are set):
- thread type `crontal` (a built-in `ThreadType` value; instructions stay in
  the settings thread defaults);
- `/ingest/crontal` (`web.ingest.crontal_ingest_router`) when the webhook
  secret is set;
- `CrontalApiTools` (`crontal_api_*`) when URL and token are set;
- the curated `crontal.*` MCP tools, registered by a background task once
  Crontal is reachable, when the URL is set;
- the Crontal notification sink (`sink.py`) when URL and secret are set.
"""
from __future__ import annotations

from typing import Any

from ..base import (
    BackgroundTask,
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

    def thread_types(self) -> list[ThreadTypeSpec]:
        return [ThreadTypeSpec(name="crontal", lane="background", visibility="shared",
                               acts_as="instance", label="Crontal")]

    def routers(self, ctx: IntegrationContext) -> list[Any]:
        if not self.webhook_secret:
            return []
        from ...web.ingest import crontal_ingest_router

        return [crontal_ingest_router(ctx.store, self.webhook_secret,
                                      harness=ctx.settings.harness_source("crontal"))]

    def tool_sets(self, ctx: IntegrationContext) -> list[Any]:
        if not (self.crontal_url and self.agent_token):
            return []
        from ...gateway.crontal_api import CrontalApiTools, crontal_base_url
        from ...run import thread_slug

        workspaces_dir = ctx.workspaces_dir
        return [CrontalApiTools(crontal_base_url(self.crontal_url), self.agent_token,
                                workspace_for=lambda key: workspaces_dir / thread_slug(key))]

    def sinks(self, ctx: IntegrationContext) -> list[Any]:
        from .sink import crontal_sink_from_config

        sink = crontal_sink_from_config(self.crontal_url, self.agent_token,
                                        self.webhook_secret, ctx.instance_name)
        return [sink] if sink is not None else []

    def background_tasks(self, ctx: IntegrationContext) -> list[BackgroundTask]:
        if not self.crontal_url:
            return []
        from .connector import run_crontal_connector

        url, token, gateway = self.crontal_url, self.agent_token, ctx.gateway

        async def run(stop):
            await run_crontal_connector(url, token, gateway, stop)

        return [BackgroundTask("crontal-connector", run)]
