import asyncio
import json
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext

from handler.runtime.harness import openai_agents as oa
from handler.runtime.harness.base import RunEventKind, RunRequest


class Responses:
    def __init__(self):
        self.calls = []
        self.body = {"result": "order", "needs_review": False, "issues": [], "evidence": ["order"]}
        self.status = "completed"
        self.error = None

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(id="resp_aux", model=kwargs["model"] + "-actual",
                               output_text=json.dumps(self.body), status=self.status,
                               usage=SimpleNamespace(input_tokens=30, output_tokens=8))


async def invoke(tool, **kwargs):
    arguments = json.dumps({"task_type": "classify", "instructions": "Classify this.",
                            "source_text": "An order.", **kwargs})
    context = ToolContext(context=None, tool_name=tool.name, tool_call_id="call1", tool_arguments=arguments)
    return json.loads(await tool.on_invoke_tool(context, arguments))


async def test_bounded_helper_is_stateless_tool_free_and_metered():
    responses = Responses()
    usage = []
    tool = oa.bounded_task_tool(SimpleNamespace(responses=responses), {"classify": "gpt-6-luna"}, usage)
    result = await invoke(tool)
    assert result["result"] == "order" and not result["needs_review"]
    [request] = responses.calls
    assert request["model"] == "gpt-6-luna"
    assert request["store"] is False and request["tools"] == []
    assert request["max_output_tokens"] == 1500
    assert "conversation" not in request and "previous_response_id" not in request
    assert request["text"]["format"]["strict"] is True
    [event] = usage
    assert event.data["model"] == "gpt-6-luna-actual" and event.data["single_request"]
    assert event.usage.input_tokens == 30
    assert event.data["tool_call_id"] == "call1"
    assert event.data["tool_name"] == "bounded_model_task"
    assert event.data["requested_model"] == "gpt-6-luna"
    assert event.data["request_id"] == "resp_aux"
    assert event.data["status"] == "completed"
    assert event.data["started_at"] <= event.data["ended_at"]
    assert event.at.isoformat() == event.data["ended_at"]


async def test_bounded_helper_rejects_oversized_disabled_and_excess_calls_without_request():
    responses = Responses()
    tool = oa.bounded_task_tool(SimpleNamespace(responses=responses), {"classify": "cheap"}, [])
    assert (await invoke(tool, source_text="x" * 4_001))["needs_review"]
    assert (await invoke(tool, instructions="x" * 2001))["needs_review"]
    assert (await invoke(tool, task_type="draft"))["needs_review"]
    assert not responses.calls
    for _ in range(6):
        await invoke(tool)
    assert (await invoke(tool))["needs_review"]
    assert len(responses.calls) == 6


async def test_workspace_paths_pass_source_without_copying_into_tool_arguments(tmp_path):
    responses = Responses()
    source = tmp_path / "order.txt"
    source.write_text("A confirmed order.")
    tool = oa.bounded_task_tool(SimpleNamespace(responses=responses), {"classify": "cheap"}, [], tmp_path)
    assert not (await invoke(tool, source_text="", source_paths=["order.txt"]))["needs_review"]
    [request] = responses.calls
    assert "A confirmed order." in json.loads(request["input"])["source_text"]
    assert source.read_text() == "A confirmed order."


@pytest.mark.parametrize("case", ["escape", "symlink", "missing", "too_many", "large", "total", "empty", "scan"])
async def test_workspace_source_failures_fall_back_without_model_calls(tmp_path, monkeypatch, case):
    responses = Responses()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("order")
    (workspace / "link.txt").symlink_to(tmp_path / "outside.txt")
    (workspace / "large.txt").write_text("order" * 10_001)
    (workspace / "one.txt").write_text("order" * 6000)
    (workspace / "two.txt").write_text("order" * 6000)
    (workspace / "scan.pdf").write_bytes(b"fake PDF")
    paths = {"escape": ["../outside.txt"], "symlink": ["link.txt"], "missing": ["missing.txt"],
             "too_many": ["one.txt"] * 6, "large": ["large.txt"],
             "total": ["one.txt", "two.txt"], "empty": [], "scan": ["scan.pdf"]}[case]
    if case == "scan":
        monkeypatch.setattr(oa.documents, "to_text", lambda *args, **kwargs: None)
    tool = oa.bounded_task_tool(SimpleNamespace(responses=responses), {"classify": "cheap"}, [], workspace)
    result = await invoke(tool, source_text="", source_paths=paths)
    assert result["needs_review"] and not responses.calls


@pytest.mark.parametrize("body,status", [("invalid", "completed"),
    ({"result": "ok", "needs_review": False, "issues": [], "evidence": ["invented"]}, "completed"),
    ({}, "incomplete")])
async def test_invalid_or_incomplete_responses_require_review_and_keep_usage(body, status):
    responses = Responses()
    responses.body, responses.status = body, status
    usage = []
    tool = oa.bounded_task_tool(SimpleNamespace(responses=responses), {"classify": "cheap"}, usage)
    assert (await invoke(tool))["needs_review"]
    assert len(usage) == 1


async def test_api_failure_falls_back_without_fabricated_usage():
    responses = Responses()
    responses.error = RuntimeError("not available")
    usage = []
    tool = oa.bounded_task_tool(SimpleNamespace(responses=responses), {"classify": "cheap"}, usage)
    assert (await invoke(tool))["needs_review"]
    assert usage == [] and len(responses.calls) == 1


@pytest.mark.parametrize("fail", [False, True, "cancel"])
async def test_harness_emits_helper_usage_on_success_and_failure(tmp_path, monkeypatch, fail):
    responses = Responses()
    client = SimpleNamespace(responses=responses)
    harness = oa.OpenAIAgentsHarness("k", client=client)
    stream_usage = SimpleNamespace(requests=1, input_tokens=100, output_tokens=10)

    class Stream:
        final_output = "verified"
        context_wrapper = SimpleNamespace(usage=stream_usage)

        def __init__(self, helper):
            self.helper = helper

        async def stream_events(self):
            await invoke(self.helper)
            if fail == "cancel":
                raise asyncio.CancelledError()
            if fail:
                raise RuntimeError("main run failed")
            if False:
                yield None

        def cancel(self):
            pass

    def run_streamed(agent, *args, **kwargs):
        helper = next(t for t in agent.tools if getattr(t, "name", None) == "bounded_model_task")
        return Stream(helper)

    monkeypatch.setattr(oa.Runner, "run_streamed", run_streamed)
    request = RunRequest(run_id="r", thread_key="chat:x", workspace=tmp_path, home=tmp_path,
                         prompt="classify", session_id="conv_1", bounded_task_models={"classify": "cheap"})
    events = []
    try:
        async for event in harness.run(request):
            events.append(event)
    except (oa.HarnessError, asyncio.CancelledError):
        assert fail
    usage = [e for e in events if e.kind == RunEventKind.USAGE]
    assert [u.data["model"] for u in usage] == ["cheap-actual", harness.default_model]


async def test_mcp_error_metadata_marks_tool_result_failed():
    harness = oa.OpenAIAgentsHarness("k", client=SimpleNamespace())
    item = SimpleNamespace(raw_item={"type": "function_call_output", "call_id": "call1"},
                           output="invalid request", custom_data=oa._mcp_result_data(SimpleNamespace(is_error=True)))

    class Stream:
        async def stream_events(self):
            yield SimpleNamespace(type="run_item_stream_event", name="tool_output", item=item)

    [event] = [e async for e in harness._events(Stream())]
    assert event.data["status"] == "error"


async def test_helper_links_source_request_and_deduplicates_response_ids():
    responses = Responses()
    usage = []
    seen = set()
    tool = oa.bounded_task_tool(SimpleNamespace(responses=responses), {"classify": "cheap"},
                               usage, call_requests={"call1": "resp_parent"}, seen_requests=seen)
    assert "ctx" not in tool.params_json_schema["properties"]
    await invoke(tool)
    await invoke(tool)
    assert len(responses.calls) == 2 and len(usage) == 1
    assert usage[0].data["source_request_id"] == "resp_parent"
    assert seen == {"resp_aux"}
