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
from handler.runtime.run import _missing_session, transcript_message
from handler.runtime.settings import HarnessName, SettingsService, resolve_harness
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import Role, TokenUsage


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
    assert finished["input"] == {}
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


async def test_usage_is_reported_per_request_with_cache_writes(tmp_path, runner):
    from agents.usage import Usage
    from openai.types.responses.response_usage import ResponseUsage

    def request(inp, cached, written, out, reasoning):
        api = ResponseUsage.model_validate({
            "input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
            "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
            "output_tokens_details": {"reasoning_tokens": reasoning}})
        return Usage(requests=1, input_tokens=inp, output_tokens=out, total_tokens=inp + out,
                     input_tokens_details=api.input_tokens_details,
                     output_tokens_details=api.output_tokens_details)

    total = Usage()
    total.add(request(1000, 0, 900, 50, 20))
    total.add(request(1200, 900, 250, 40, 10))
    stream = FakeStream(_events())
    stream.context_wrapper = SimpleNamespace(usage=total)
    runner.state["next"] = stream
    harness = oa.OpenAIAgentsHarness("k", default_model="gpt-6.1-sol", client=FakeClient())
    usage = [e for e in await _collect(harness, _req(tmp_path)) if e.kind == RunEventKind.USAGE]

    assert [e.data for e in usage] == [{"model": "gpt-6.1-sol", "single_request": True}] * 2
    assert [e.usage for e in usage] == [TokenUsage(1000, 0, 900, 50, 20),
                                        TokenUsage(1200, 900, 250, 40, 10)]
    assert [e.raw["request"] for e in usage] == [1, 2]
    # the SDK's own sum loses cache writes; the per-request events keep them
    assert sum(e.usage.cache_write_tokens for e in usage) == 1150


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


PNG_BYTES = b"\x89PNG\r\n\x1a\n fake"
JPEG_BYTES = b"\xff\xd8\xff\xe0 fake"


async def test_images_are_sent_as_input_images(tmp_path, runner):
    image = tmp_path / "shot.png"
    image.write_bytes(PNG_BYTES)
    # the type comes from the bytes: a pasted "image.png" may really be a JPEG
    mislabeled = tmp_path / "image.png"
    mislabeled.write_bytes(JPEG_BYTES)
    harness = oa.OpenAIAgentsHarness("k", client=FakeClient())
    await _collect(harness, _req(tmp_path, images=[image, mislabeled]))
    (message,) = runner.calls[0]["input"]
    assert message["role"] == "user"
    text, img, jpg = message["content"]
    assert text == {"type": "input_text", "text": "how many RFQs?"}
    assert img == {"type": "input_image",
                   "image_url": "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()}
    assert jpg["image_url"] == "data:image/jpeg;base64," + base64.b64encode(JPEG_BYTES).decode()


async def test_text_only_turn_is_a_plain_string(tmp_path, runner):
    harness = oa.OpenAIAgentsHarness("k", client=FakeClient())
    await _collect(harness, _req(tmp_path))
    assert runner.calls[0]["input"] == "how many RFQs?"


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


async def test_tool_arguments_reach_the_transcript(tmp_path, runner):
    calls = [
        SimpleNamespace(raw_item={"type": "function_call", "call_id": "c1",
                                  "name": "crontal_get_rfq", "arguments": '{"rfq_id": 7}'}),
        SimpleNamespace(raw_item={"type": "function_call", "call_id": "c2",
                                  "name": "list_files", "arguments": "{}"}),
    ]
    outputs = [SimpleNamespace(raw_item={"type": "function_call_output", "call_id": c},
                               output="ok") for c in ("c2", "c1")]
    search = SimpleNamespace(raw_item={"type": "web_search_call", "id": "ws1",
                                       "status": "completed",
                                       "action": {"type": "search", "query": "steel"}})
    runner.state["next"] = FakeStream([
        _item_event("tool_called", calls[0]), _item_event("tool_called", calls[1]),
        _item_event("tool_output", outputs[0]), _item_event("tool_output", outputs[1]),
        _item_event("tool_called", search),
    ])
    events = await _collect(oa.OpenAIAgentsHarness("k", client=FakeClient()), _req(tmp_path))

    inputs: dict = {}
    messages = [m for ev in events
                if (m := transcript_message("t", "run_1", ev, inputs)) is not None]
    calls_by_id = {m.blocks[0]["id"]: m.blocks[0] for m in messages if m.role == Role.TOOL}
    assert calls_by_id["c1"]["name"] == "crontal_get_rfq"
    assert calls_by_id["c1"]["input"] == {"rfq_id": 7}
    assert calls_by_id["c2"]["name"] == "list_files" and calls_by_id["c2"]["input"] == {}
    assert calls_by_id["ws1"]["input"] == {"type": "search", "query": "steel"}
    assert inputs == {}


async def test_defaults_report_what_a_bare_request_uses():
    harness = oa.OpenAIAgentsHarness("k", default_reasoning_effort="low")
    assert await harness.defaults() == {"model": "gpt-6.1-sol", "reasoning_effort": "low"}
    harness = oa.OpenAIAgentsHarness("k", default_model="gpt-6-luna")
    assert await harness.defaults() == {"model": "gpt-6-luna", "reasoning_effort": None}


# --- conversation repair, usage on failure, cancellation, truncation ------------------

class FakeItems:
    def __init__(self, items: list[dict]) -> None:
        self.items = [SimpleNamespace(**item) for item in items]
        self.created: list[list[dict]] = []

    def list(self, conversation_id: str, **kwargs: Any):
        async def pages():
            for item in list(self.items):
                yield item
        return pages()

    async def create(self, conversation_id: str, *, items: list[dict]) -> None:
        self.created.append(items)
        self.items += [SimpleNamespace(**item) for item in items]


def _client_with(items: list[dict]) -> FakeClient:
    client = FakeClient()
    client.conversations.items = FakeItems(items)
    return client


BROKEN_ITEMS = [
    {"type": "message", "id": "m1"},
    {"type": "function_call", "call_id": "call_ok", "name": "x"},
    {"type": "function_call_output", "call_id": "call_ok"},
    {"type": "function_call", "call_id": "call_lost", "name": "crontal_api_get"},
]


def _missing_output_error():
    return _api_error(openai.BadRequestError,
                      "No tool output found for function call call_lost.", 400)


def _sequence(runner, monkeypatch, streams: list) -> None:
    def run_streamed(agent, input, **kwargs):
        runner.calls.append({"agent": agent, "input": input, **kwargs})
        runner.state["stream"] = streams.pop(0)
        return runner.state["stream"]

    monkeypatch.setattr(oa.Runner, "run_streamed", staticmethod(run_streamed))


async def test_repair_adds_placeholder_outputs_for_unanswered_calls():
    client = _client_with(BROKEN_ITEMS + [{"type": "custom_tool_call", "call_id": "c2"}])
    assert await oa.repair_conversation(client, "conv_9") == 2
    (added,) = client.conversations.items.created
    assert added == [
        {"type": "function_call_output", "call_id": "call_lost", "output": oa.INTERRUPTED_OUTPUT},
        {"type": "custom_tool_call_output", "call_id": "c2", "output": oa.INTERRUPTED_OUTPUT},
    ]
    assert await oa.repair_conversation(client, "conv_9") == 0  # idempotent


async def test_missing_tool_output_is_repaired_and_retried_once(tmp_path, runner, monkeypatch):
    client = _client_with(BROKEN_ITEMS)
    _sequence(runner, monkeypatch,
              [FakeStream([], error=_missing_output_error()), FakeStream(_events())])
    harness = oa.OpenAIAgentsHarness("k", client=client)
    events = await _collect(harness, _req(tmp_path, session_id="conv_9"))
    assert len(runner.calls) == 2
    assert client.conversations.items.created[0][0]["call_id"] == "call_lost"
    assert events[-1].kind is RunEventKind.FINAL
    assert not any(e.kind is RunEventKind.ERROR for e in events)


async def test_unrepairable_missing_output_fails_without_looping(tmp_path, runner):
    # Every call already has its output: the 400 is raised, not retried.
    client = _client_with(BROKEN_ITEMS[:3])
    runner.state["next"] = FakeStream([], error=_missing_output_error())
    harness = oa.OpenAIAgentsHarness("k", client=client)
    with pytest.raises(HarnessError):
        await _collect(harness, _req(tmp_path, session_id="conv_9"))
    assert len(runner.calls) == 1


async def test_missing_output_after_progress_repairs_but_does_not_replay(tmp_path, runner):
    client = _client_with(BROKEN_ITEMS)
    runner.state["next"] = FakeStream(_events()[:2], error=_missing_output_error())
    harness = oa.OpenAIAgentsHarness("k", client=client)
    with pytest.raises(HarnessError):
        await _collect(harness, _req(tmp_path, session_id="conv_9"))
    assert len(runner.calls) == 1  # the turn already ran tools; it is not sent twice
    assert client.conversations.items.created  # but the next turn will be accepted


async def test_repair_session_flag_repairs_before_the_turn(tmp_path, runner):
    client = _client_with(BROKEN_ITEMS)
    harness = oa.OpenAIAgentsHarness("k", client=client)
    await _collect(harness, _req(tmp_path, session_id="conv_9", repair_session=True))
    assert client.conversations.items.created[0][0]["call_id"] == "call_lost"
    assert len(runner.calls) == 1


async def test_failed_run_still_reports_usage(tmp_path, runner):
    runner.state["next"] = FakeStream(_events()[:2],
                                      error=_api_error(openai.BadRequestError, "bad", 400))
    harness = oa.OpenAIAgentsHarness("k", default_model="gpt-6.1-sol", client=FakeClient())
    events: list = []
    with pytest.raises(HarnessError):
        async for ev in harness.run(_req(tmp_path)):
            events.append(ev)
    assert [e.kind for e in events][-2:] == [RunEventKind.USAGE, RunEventKind.ERROR]
    assert events[-2].usage.input_tokens == 120 and events[-2].usage.output_tokens == 30


async def test_cancelled_run_cancels_the_sdk_run(tmp_path, runner):
    import asyncio

    class Hanging(FakeStream):
        async def stream_events(self):
            yield _events()[1]
            await asyncio.Event().wait()

    runner.state["next"] = stream = Hanging([])
    harness = oa.OpenAIAgentsHarness("k", client=FakeClient())

    async def consume():
        async for _ in harness.run(_req(tmp_path)):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.cancelled


async def test_completed_run_is_not_cancelled(tmp_path, runner):
    harness = oa.OpenAIAgentsHarness("k", client=FakeClient())
    await _collect(harness, _req(tmp_path))
    assert runner.state["stream"].cancelled is False


async def test_truncation_and_compaction_are_on(tmp_path, runner):
    harness = oa.OpenAIAgentsHarness("k", client=FakeClient(), compact_threshold=150_000)
    await _collect(harness, _req(tmp_path))
    settings = runner.calls[0]["agent"].model_settings
    assert settings.truncation == "auto"
    assert settings.context_management == [{"type": "compaction", "compact_threshold": 150_000}]
    off = oa.OpenAIAgentsHarness("k", client=FakeClient(), compact_threshold=0)
    assert off._agent(_req(tmp_path), []).model_settings.context_management is None


async def test_refused_compaction_is_turned_off_and_retried(tmp_path, runner, monkeypatch):
    _sequence(runner, monkeypatch, [
        FakeStream([], error=_api_error(
            openai.BadRequestError, "Unknown parameter: 'context_management'.", 400)),
        FakeStream(_events())])
    harness = oa.OpenAIAgentsHarness("k", client=FakeClient(), compact_threshold=150_000)
    events = await _collect(harness, _req(tmp_path, session_id="conv_9"))
    assert events[-1].kind is RunEventKind.FINAL
    assert harness.compact_threshold is None
    assert runner.calls[1]["agent"].model_settings.context_management is None
