"""Native request usage survives failures and never duplicates SDK aggregates."""
import asyncio
from types import SimpleNamespace as NS

import pytest
from agents.usage import Usage

from handler.runtime.harness import openai_agents as oa
from handler.runtime.harness.base import RunEventKind, RunRequest


def response(request_id="resp_1", inp=100, out=10, status="completed"):
    return NS(id=request_id, model="actual-model", status=status, service_tier="default",
              created_at=1794170000, output=[NS(call_id="call_1", id="fc_1")],
              usage=NS(input_tokens=inp, output_tokens=out))


def raw(kind, value):
    return NS(type="raw_response_event", data=NS(type=kind, response=value))


def sdk_usage(*values):
    total = Usage()
    for inp, out in values:
        total.add(Usage(requests=1, input_tokens=inp, output_tokens=out, total_tokens=inp+out))
    return total


@pytest.mark.parametrize("terminal", ["completed", "incomplete", "failed"])
@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
async def test_meter_reports_before_tool_and_survives_failures(tmp_path, monkeypatch, terminal, failure):
    value = response(status=terminal)
    class Stream:
        final_output = "done"
        context_wrapper = NS(usage=sdk_usage((100, 10)))
        async def stream_events(self):
            yield raw("response.created", value)
            yield raw("response." + terminal, value)
            yield NS(type="run_item_stream_event", name="tool_called",
                     item=NS(raw_item={"type": "function_call", "call_id": "call_1",
                                       "name": "read_file", "arguments": "{}"}))
            if failure:
                raise failure()
        def cancel(self):
            pass
    monkeypatch.setattr(oa.Runner, "run_streamed", lambda *a, **k: Stream())
    harness = oa.OpenAIAgentsHarness("k", client=NS())
    req = RunRequest(run_id="r", thread_key="chat:u:t", workspace=tmp_path, home=tmp_path,
                     prompt="read", model="requested", session_id="conv_1")
    events = []
    try:
        async for event in harness.run(req):
            events.append(event)
    except (oa.HarnessError, asyncio.CancelledError):
        assert failure
    [usage] = [e for e in events if e.kind == RunEventKind.USAGE]
    assert usage.data["model"] == "actual-model"
    assert usage.data["requested_model"] == "requested"
    assert usage.data["status"] == terminal
    assert usage.data["request_id"] == "resp_1"
    assert usage.data["service_tier"] == "default"
    assert usage.data["metering_quality"] == "provider_reported"
    assert usage.at.isoformat() == usage.data["ended_at"]
    tool = next(e for e in events if e.kind == RunEventKind.TOOL_STARTED)
    assert tool.data["request_id"] == "resp_1"
    assert events.index(usage) < events.index(tool)


def test_sdk_only_compaction_and_aggregate_residual_are_retained():
    meter = oa._RequestMeter("requested", False, set(), {})
    assert meter.response(NS(type="response.completed", response=response()))
    [fallback] = meter.fallback(NS(context_wrapper=NS(usage=sdk_usage((50, 5), (100, 10)))))
    assert fallback.usage.input_tokens == 50
    assert fallback.data["metering_quality"] == "sdk_fallback"
    assert fallback.data["single_request"]
    aggregate = NS(requests=2, input_tokens=150, output_tokens=15)
    [fallback] = meter.fallback(NS(context_wrapper=NS(usage=aggregate)))
    assert fallback.usage.input_tokens == 50 and fallback.usage.output_tokens == 5
    assert not fallback.data.get("single_request")


def test_duplicate_response_ids_are_not_billed_again_across_recovery():
    seen = set()
    for i in range(2):
        meter = oa._RequestMeter("requested", False, seen, {})
        event = meter.response(NS(type="response.completed", response=response()))
        assert bool(event) == (i == 0)
        assert meter.response(NS(type="response.completed", response=response())) is None
        assert meter.fallback(NS(context_wrapper=NS(usage=sdk_usage((100, 10))))) == []


def test_provider_time_used_when_created_event_absent():
    event = oa._RequestMeter("requested", False, set(), {}).response(
        NS(type="response.completed", response=response()))
    assert event.data["timestamp_quality"] == "provider_created_at"
    assert event.data["started_at"] is not None
