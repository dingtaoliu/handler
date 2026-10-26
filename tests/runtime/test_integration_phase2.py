"""Phase 2 wiring: executor ↔ gateway tokens, settings-driven plans, approval routing."""

from __future__ import annotations

import asyncio
from pathlib import Path

from handler.runtime.bus import RunEventBus
from handler.runtime.dispatcher import Dispatcher
from handler.runtime.gateway.gateway import Gateway
from handler.runtime.gateway.policy import GrantPolicy
from handler.runtime.gateway.tokens import RunTokens
from handler.runtime.harness.base import RunEvent, RunEventKind, RunRequest
from handler.runtime.memory import MemoryTools
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.run import RunConfig, RunExecutor
from handler.runtime.server import thread_router
from handler.runtime.settings import SettingsService
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import (
    ApprovalStatus,
    Event,
    Lane,
    MemoryTopic,
    Role,
    ThreadSpec,
    ThreadType,
)


class RecordingHarness:
    """Captures each request and resolves its MCP token while the run is live."""

    name = "fake"

    def __init__(self, tokens: RunTokens) -> None:
        self.tokens = tokens
        self.requests: list[RunRequest] = []
        self.live_contexts = []

    async def run(self, req: RunRequest):
        self.requests.append(req)
        token = req.mcp_servers[0].headers["Authorization"].removeprefix("Bearer ")
        self.live_contexts.append(self.tokens.resolve(token))
        yield RunEvent(RunEventKind.SESSION, {"session_id": "s1"})
        yield RunEvent(RunEventKind.ASSISTANT_TEXT, {"text": "ok"})
        yield RunEvent(RunEventKind.FINAL, {"text": "ok", "session_id": "s1"})

    async def interrupt(self, run_id: str) -> None:
        pass


async def _drain(dispatcher: Dispatcher) -> None:
    await dispatcher.claim_once()
    while dispatcher.active_count:
        await asyncio.sleep(0.01)


async def test_run_gets_gateway_token_instructions_and_settings(tmp_path: Path):
    store = SQLiteStore(tmp_path / "h.db")
    settings = SettingsService(store)
    await settings.put("persona", {"system": "", "identity": "You are Handler, an ops agent.",
                                   "persona": "Be brief."}, updated_by="t", expected_version=None)
    await settings.put("threads", {"chat": {"harness": "codex", "model": "gpt-6-luna",
                                            "reasoning_effort": "medium"}},
                       updated_by="t", expected_version=None)
    memory = MemoryTools(store)
    await store.put_topic(MemoryTopic("people/danny", "Danny's preferences", "likes tables"), None)
    tokens = RunTokens()
    harness = RecordingHarness(tokens)
    executor = RunExecutor(store, {"fake": harness}, RunEventBus(),
                           RunConfig(data_dir=tmp_path / "d"), PriceTable([]),
                           tokens=tokens, mcp_url="http://127.0.0.1:9/mcp/",
                           settings=settings, memory_tools=memory)
    dispatcher = Dispatcher(store, executor)

    ev = Event(type="chat.message", source="t", payload={"text": "hi"}, idempotency_key="k1",
               thread_key="chat:danny:c1", user_id="danny")
    await store.put(ev, ThreadSpec("chat:danny:c1", ThreadType.CHAT, Lane.INTERACTIVE,
                                   owner_user_id="danny", harness="fake"))
    await _drain(dispatcher)

    (req,) = harness.requests
    assert req.model == "gpt-6-luna" and req.reasoning_effort == "medium"
    assert "You are Handler" in req.instructions and "people/danny: Danny's preferences" in req.instructions
    assert "You are talking with danny" in req.instructions
    assert req.mcp_servers[0].url == "http://127.0.0.1:9/mcp/"
    live = harness.live_contexts[0]
    assert live is not None and live.thread_key == "chat:danny:c1" and live.acting_as == "danny"
    token = req.mcp_servers[0].headers["Authorization"].removeprefix("Bearer ")
    assert tokens.resolve(token) is None  # revoked when the run ended
    await store.close()


async def test_approval_resolution_is_routed_into_the_requesting_thread(tmp_path: Path):
    store = SQLiteStore(tmp_path / "h.db")
    key = "chat:danny:c2"
    await store.put(
        Event(type="chat.message", source="t", payload={"text": "send it"}, idempotency_key="k2",
              thread_key=key, user_id="danny"),
        ThreadSpec(key, ThreadType.CHAT, Lane.INTERACTIVE, owner_user_id="danny", harness="fake"),
    )
    gateway = Gateway(store, GrantPolicy({"actions": {"memory.save": "ask"}}),
                      emit_event=thread_router(store))
    gateway.register(MemoryTools(store))
    from handler.runtime.context import RunContext

    ctx = RunContext(run_id="r1", thread_key=key, user_id="danny", acting_as="danny")
    pending = await gateway.call(ctx, "memory_save", {"name": "notes", "content": "x",
                                                      "description": "d"})
    approval_id = pending.content["approval_id"]
    approval = await gateway.approve(approval_id, decided_by="danny")
    assert approval.status is ApprovalStatus.EXECUTED
    assert (await MemoryTools(store).get_visible("danny", "notes")).content == "x"

    events = await store.pending_events(key)
    assert [e.type for e in events] == ["chat.message", "approval.resolved"]
    assert events[1].payload["approval_id"] == approval_id
    await store.close()
