"""Fake integrations used by the integration registry tests."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

from handler.runtime.gateway.base import ToolSpec
from handler.runtime.types import Risk
from handler.runtime.integrations import (
    BackgroundTask,
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
    started: list[str] = []

    def configure(self, config):
        super().configure(config)
        self.token = config.require("ALPHA_TOKEN")

    def thread_types(self):
        return [ThreadTypeSpec("alpha", label="Alpha")]

    def ui_entries(self):
        return [UIManifestEntry("connection", "alpha", "Alpha")]

    def routers(self, ctx):
        router = APIRouter()
        token = self.token

        @router.get("/ingest/alpha")
        async def ingest():
            return {"instance": ctx.instance_name, "token": token}

        return [router]

    def tool_sets(self, ctx):
        return [tool("alpha_ping")]

    def sinks(self, ctx):
        self.sink = FakeSink("alpha.sink")
        return [self.sink]

    def identity_provider(self, ctx):
        return object()

    def background_tasks(self, ctx):
        async def run(stop: asyncio.Event):
            Alpha.started.append(ctx.instance_name)
            await stop.wait()
        return [BackgroundTask("poller", run)]


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
