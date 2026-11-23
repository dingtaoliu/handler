"""Fake integrations used by the integration registry tests."""
from __future__ import annotations

from fastapi import APIRouter

from handler.runtime.gateway.base import ToolSpec
from handler.runtime.types import Risk
from handler.runtime.integrations import (
    Integration,
    IntegrationRegistry,
    ThreadTypeSpec,
    UIManifestEntry,
)
from handler.runtime.sinks import RetryPolicy


async def _noop(ctx, args):
    return {"ok": True}


def tool(name: str) -> ToolSpec:
    return ToolSpec(name=name, action=name.replace("_", "."), risk=Risk.READ,
                    description=name, input_schema={"type": "object", "properties": {}},
                    handler=_noop)


class FakeSink:
    def __init__(self, name: str = "fake.sink") -> None:
        self.name = name
        self.retry = RetryPolicy()
        self.delivered: list = []

    def accepts(self, event) -> bool:
        return True

    async def deliver(self, payload) -> None:
        self.delivered.append(payload)


class Alpha(Integration):
    """Contributes one of everything."""

    name = "alpha"
    label = "Alpha"
    loaded: list[str] = []

    def configure(self, config):
        super().configure(config)
        self.token = config.require("ALPHA_TOKEN")

    def thread_types(self):
        return [ThreadTypeSpec("alpha", label="Alpha")]

    def ui_entries(self):
        return [UIManifestEntry("connection", "alpha", "Alpha")]

    def routers(self, tenant):
        router = APIRouter()

        @router.get("/ingest/alpha")
        async def ingest():
            return {"instance": tenant.context().instance_name,
                    "token": tenant.integration().token}

        return [router]

    def tool_sets(self, ctx):
        return [tool("alpha_ping")]

    def sinks(self, ctx):
        self.sink = FakeSink("alpha.sink")
        return [self.sink]

    def identity_provider(self, ctx):
        return object()

    async def load(self, ctx):
        Alpha.loaded.append(ctx.instance_name)
        return True


class Beta(Integration):
    name = "beta"
    requires = ("alpha",)


class Gamma(Integration):
    name = "gamma"

    def identity_provider(self, ctx):
        return object()


def registry(*classes) -> IntegrationRegistry:
    reg = IntegrationRegistry()
    for cls in classes or (Alpha, Beta, Gamma):
        reg.register(cls)
    return reg
