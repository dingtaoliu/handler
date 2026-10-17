import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from handler.runtime.gateway import Gateway, GrantPolicy, ToolResult, ToolSpec
from handler.runtime.store.sqlite import SQLiteCore
from handler.runtime.store.sqlite_gateway import ApprovalMixin, AuditMixin
from handler.runtime.types import Risk


class GatewayStore(ApprovalMixin, AuditMixin, SQLiteCore):
    pass


@dataclass
class Clock:
    now: datetime = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Tools:
    """A tool provider that counts executions."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def tools(self) -> list[ToolSpec]:
        async def echo(ctx, args):
            self.calls.append(args)
            await asyncio.sleep(0.01)
            return ToolResult({"echo": args["text"], "run": ctx.run_id, "as": ctx.acting_as})

        async def boom(ctx, args):
            raise RuntimeError("kaboom")

        async def slow(ctx, args):
            await asyncio.sleep(5)
            return ToolResult("late")

        def spec(name, action, risk, handler):
            return ToolSpec(
                name=name, action=action, risk=risk, description=f"{name} tool",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=handler,
            )

        return [
            spec("echo", "test.echo", Risk.READ, echo),
            spec("send_it", "test.send", Risk.WRITE_EXTERNAL, echo),
            spec("boom", "test.boom", Risk.READ, boom),
            spec("slow", "test.slow", Risk.READ, slow),
            spec("blocked", "test.blocked", Risk.READ, echo),
        ]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def store(tmp_path, clock):
    s = GatewayStore(tmp_path / "gw.db", clock=clock)
    yield s


@pytest.fixture
def tools():
    return Tools()


@pytest.fixture
def emitted():
    return []


@pytest.fixture
def gateway(store, tools, clock, emitted):
    async def emit(event):
        emitted.append(event)

    policy = GrantPolicy({"actions": {"test.blocked": "deny"}})
    gw = Gateway(store, policy, emit_event=emit, call_timeout_seconds=0.3, clock=clock)
    gw.register(tools)
    return gw
