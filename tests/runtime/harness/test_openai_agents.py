from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from handler.runtime.harness import openai_agents as oa
from handler.runtime.harness.base import (
    HarnessError,
    McpServerSpec,
    RunEventKind,
    RunRequest,
)
from handler.runtime.run import _missing_session
from handler.runtime.settings import HarnessName, SettingsService, resolve_harness
from handler.runtime.store.sqlite import SQLiteStore


class FakeConversations:
    def __init__(self) -> None:
        self.created = 0

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.created += 1
        return SimpleNamespace(id=f"conv_{self.created}")


class FakeClient:
    def __init__(self) -> None:
        self.conversations = FakeConversations()


def _item_event(name: str, item: Any) -> SimpleNamespace:
    return SimpleNamespace(type="run_item_stream_event", name=name, item=item)


class FakeStream:
    def __init__(self, events: list, error: Exception | None = None) -> None:
        self._events, self._error = events, error
        usage = SimpleNamespace(
            requests=2, input_tokens=120, output_tokens=30,
            input_tokens_details=SimpleNamespace(cached_tokens=40),
            output_tokens_details=SimpleNamespace(reasoning_tokens=5))
        self.context_wrapper = SimpleNamespace(usage=usage)
        self.final_output = "You have 1 RFQ."
        self.cancelled = False

    async def stream_events(self):
        for event in self._events:
            yield event
        if self._error is not None:
            raise self._error

    def cancel(self) -> None:
        self.cancelled = True


def _events() -> list:
    call = SimpleNamespace(raw_item={"type": "function_call", "call_id": "c1",
                                     "name": "crontal_get_rfqs", "arguments": "{}"})
    output = SimpleNamespace(raw_item={"type": "function_call_output", "call_id": "c1"},
                             output='{"success": true}')
    message = SimpleNamespace(raw_item={"type": "message", "role": "assistant"})
    return [
        SimpleNamespace(type="raw_response_event"),
        _item_event("tool_called", call),
        _item_event("tool_output", output),
        _item_event("message_output_created", message),
    ]


@pytest.fixture
def runner(monkeypatch):
    calls: list[dict] = []
    state: dict[str, Any] = {"stream": None}

    def run_streamed(agent, input, **kwargs):
        calls.append({"agent": agent, "input": input, **kwargs})
        state["stream"] = state.get("next") or FakeStream(_events())
        return state["stream"]

    monkeypatch.setattr(oa.Runner, "run_streamed", staticmethod(run_streamed))
    monkeypatch.setattr(oa.ItemHelpers, "text_message_output",
                        staticmethod(lambda item: "You have 1 RFQ."))
    return SimpleNamespace(calls=calls, state=state)


def _req(tmp_path: Path, **kw: Any) -> RunRequest:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return RunRequest(run_id="run_1", thread_key="chat:danny:c1", workspace=ws,
                      home=tmp_path / "home", prompt="how many RFQs?",
                      instructions="Be brief.", **kw)


async def _collect(harness, req):
    return [ev async for ev in harness.run(req)]


async def test_new_thread_creates_a_conversation_and_maps_events(tmp_path, runner):
    client = FakeClient()
    harness = oa.OpenAIAgentsHarness("k", default_model="gpt-6.1-sol",
                                     default_reasoning_effort="low", client=client)
    events = await _collect(harness, _req(tmp_path))

    kinds = [e.kind for e in events]
    assert kinds == [RunEventKind.SESSION, RunEventKind.TURN_STARTED, RunEventKind.TOOL_STARTED,
                     RunEventKind.TOOL_FINISHED, RunEventKind.ASSISTANT_TEXT,
                     RunEventKind.USAGE, RunEventKind.FINAL]
    assert events[0].data == {"session_id": "conv_1"}
    started, finished = events[2].data, events[3].data
    assert started["tool"] == "crontal_get_rfqs" and started["input"] == {}
    assert finished["tool"] == "crontal_get_rfqs" and finished["output"] == '{"success": true}'
    usage = events[5].usage
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens,
            usage.reasoning_tokens) == (120, 40, 30, 5)
    assert events[5].data == {"model": "gpt-6.1-sol"}
    assert events[-1].data == {"text": "You have 1 RFQ.", "session_id": "conv_1"}

    call = runner.calls[0]
    assert call["conversation_id"] == "conv_1" and call["input"] == "how many RFQs?"
    assert call["agent"].instructions == "Be brief."
    assert call["agent"].model_settings.reasoning.effort == "low"
    assert {t.name for t in call["agent"].tools} == {"web_search", "list_files", "read_file",
                                                     "write_file"}


async def test_resumed_thread_reuses_its_conversation(tmp_path, runner):
    client = FakeClient()
    harness = oa.OpenAIAgentsHarness("k", client=client)
    events = await _collect(harness, _req(tmp_path, session_id="conv_9", model="gpt-6-luna",
                                          reasoning_effort="high"))
    assert client.conversations.created == 0
    assert runner.calls[0]["conversation_id"] == "conv_9"
    assert runner.calls[0]["agent"].model_settings.reasoning.effort == "high"
    assert events[-1].data["session_id"] == "conv_9"
    assert events[-2].data == {"model": "gpt-6-luna"}


async def test_images_are_sent_as_input_images(tmp_path, runner):
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG fake")
    harness = oa.OpenAIAgentsHarness("k", client=FakeClient())
    await _collect(harness, _req(tmp_path, images=[image]))
    (message,) = runner.calls[0]["input"]
    text, img = message["content"]
    assert text == {"type": "input_text", "text": "how many RFQs?"}
    assert img["image_url"] == "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()


async def test_mcp_servers_connect_and_clean_up(tmp_path, runner, monkeypatch):
    seen: list[tuple[str, Any]] = []

    class FakeMCP:
        def __init__(self, params, name=None, **kw):
            seen.append(("init", params))
            self.name = name

        async def connect(self):
            seen.append(("connect", self.name))

        async def cleanup(self):
            seen.append(("cleanup", self.name))

    monkeypatch.setattr(oa, "MCPServerStreamableHttp", FakeMCP)
    harness = oa.OpenAIAgentsHarness("k", client=FakeClient())
    spec = McpServerSpec(name="handler", url="http://127.0.0.1:9/mcp/",
                         headers={"Authorization": "Bearer t"})
    await _collect(harness, _req(tmp_path, mcp_servers=[spec]))
    assert seen[0] == ("init", {"url": "http://127.0.0.1:9/mcp/",
                                "headers": {"Authorization": "Bearer t"}})
    assert ("connect", "handler") in seen and seen[-1] == ("cleanup", "handler")
    assert len(runner.calls[0]["agent"].mcp_servers) == 1


def _api_error(cls, message: str, status: int):
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com"))
    return cls(message, response=response, body=None)


async def test_rate_limits_are_retryable(tmp_path, runner):
    runner.state["next"] = FakeStream([], error=_api_error(openai.RateLimitError, "slow down", 429))
    harness = oa.OpenAIAgentsHarness("k", client=FakeClient())
    events: list = []
    with pytest.raises(HarnessError) as info:
        async for ev in harness.run(_req(tmp_path)):
            events.append(ev)
    assert info.value.retryable
    assert events[-1].kind is RunEventKind.ERROR and events[-1].data["retryable"] is True


async def test_missing_conversation_triggers_session_recovery(tmp_path, runner):
    runner.state["next"] = FakeStream(
        [], error=_api_error(openai.NotFoundError, "Conversation with id conv_9 not found", 404))
    harness = oa.OpenAIAgentsHarness("k", client=FakeClient())
    with pytest.raises(HarnessError) as info:
        await _collect(harness, _req(tmp_path, session_id="conv_9"))
    assert not info.value.retryable
    assert _missing_session(info.value, "conv_9")


async def test_unknown_model_is_not_mistaken_for_a_lost_session(tmp_path, runner):
    runner.state["next"] = FakeStream(
        [], error=_api_error(openai.NotFoundError, "The model `nope` does not exist", 404))
    harness = oa.OpenAIAgentsHarness("k", client=FakeClient())
    with pytest.raises(HarnessError) as info:
        await _collect(harness, _req(tmp_path, session_id="conv_9"))
    assert not _missing_session(info.value, "conv_9")


def test_workspace_paths_are_confined(tmp_path):
    ws = tmp_path / "ws"
    (ws / "attachments").mkdir(parents=True)
    (ws / "attachments" / "a.txt").write_text("hello")
    assert oa._confined(ws, "attachments/a.txt") == (ws / "attachments" / "a.txt").resolve()
    for bad in ("../secret", "/etc/passwd", "attachments/../../x"):
        with pytest.raises(ValueError):
            oa._confined(ws, bad)


async def test_harness_setting_is_read_live(tmp_path):
    store = SQLiteStore(tmp_path / "h.db")
    settings = SettingsService(store)
    source = settings.harness_source("chat")
    assert await resolve_harness(source) == "codex"
    threads, version = await settings.get("threads")
    threads["chat"]["harness"] = HarnessName.OPENAI_AGENTS.value
    await settings.put("threads", threads, updated_by="t", expected_version=version)
    assert await resolve_harness(source) == "openai_agents"
    assert await resolve_harness("codex") == "codex"
    await store.close()
