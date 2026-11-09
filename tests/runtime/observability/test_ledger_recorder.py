"""Recorder persists attributed provider spend independently of run completion."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from handler.runtime.harness.base import RunEvent, RunEventKind
from handler.runtime.observability.ledger import RunAttribution
from handler.runtime.observability.pricing import PriceEntry, PriceTable
from handler.runtime.observability.recorder import RawSink, StepRecorder
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import Event, Lane, Run, RunStatus, ThreadSpec, ThreadType, TokenUsage

NOW = datetime(2026, 11, 8, 15, tzinfo=timezone.utc)
THREAD = "chat:user-7:conversation"


@pytest.fixture
async def ledger(tmp_path):
    store = SQLiteStore(tmp_path / "ledger.sqlite", clock=lambda: NOW)
    await store.put(Event(type="chat.message", source="test", payload={}, idempotency_key="message",
                          thread_key=THREAD, user_id="user-7", received_at=NOW),
                    ThreadSpec(key=THREAD, type=ThreadType.CHAT, lane=Lane.INTERACTIVE,
                               owner_user_id="user-7", harness="openai_agents"))
    [lease] = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    run = Run(id="run-1", thread_key=THREAD, lease_version=lease.version, trigger_event_ids=[],
              status=RunStatus.RUNNING, harness="openai_agents", model="requested", started_at=NOW)
    assert await store.create_run(lease, run)
    attribution = RunAttribution(run_id=run.id, thread_key=THREAD, instance_name="bss",
                                 initiating_user_id="user-7", attributed_user_id="user-7",
                                 thread_owner_user_id="user-7", attribution_reason="initiating_user",
                                 captured_at=NOW)
    await store.save_run_attribution(attribution)
    prices = PriceTable([PriceEntry("*", date(2026, 1, 1), 2, .2, 3, 10)])
    def recorder():
        return StepRecorder(store, run.id, model="requested", prices=prices,
                            raw_sink=RawSink(tmp_path / "raw"), attribution=attribution,
                            harness="openai_agents")
    try:
        yield store, recorder, prices
    finally:
        await store.close()


def usage_event(**overrides):
    data = {"request_id": "resp-1", "model": "actual", "requested_model": "requested",
            "single_request": True, "metering_quality": "provider_reported",
            "status": "completed", "started_at": (NOW-timedelta(seconds=2)).isoformat(),
            "ended_at": NOW.isoformat(), "service_tier": "default",
            "tool_call_id": "call-helper", "tool_name": "bounded_model_task",
            "source_request_id": "resp-parent", "timestamp_quality": "observed", **overrides}
    return RunEvent(RunEventKind.USAGE, data, usage=TokenUsage(100, 20, 10, 8, 3), at=NOW)


async def test_provider_request_dedupes_in_memory_and_durable_store(ledger):
    store, make_recorder, prices = ledger
    recorder = make_recorder()
    event = usage_event()
    await recorder.on_event(event)
    before = recorder.totals()
    await recorder.on_event(event)
    assert recorder.totals() == before
    # A retried callback after process recovery must not create another charge.
    await make_recorder().on_event(event)
    rows = await store.list_usage_records(NOW, attributed_user_id="user-7")
    assert len(rows) == 1
    assert before[0] == event.usage
    assert before[1] == pytest.approx((70*2 + 20*.2 + 10*3 + 8*10)/1e6)
    assert len(await store.list_steps("run-1")) == 1


async def test_actual_model_timing_attribution_and_tool_linkage_round_trip(ledger):
    store, make_recorder, _ = ledger
    await make_recorder().on_event(usage_event())
    [row] = await store.list_usage_records(NOW, thread_key=THREAD, model="actual",
                                          tool_name="bounded_model_task", attributed_user_id="user-7")
    assert row.request_id == "resp-1" and row.tool_call_id == "call-helper"
    assert row.model == "actual" and row.requested_model == "requested"
    assert row.harness == "openai_agents" and row.service_tier == "default"
    assert row.started_at == NOW-timedelta(seconds=2)
    assert row.ended_at == row.occurred_at == NOW
    assert row.metering_quality == "provider_reported" and row.status == "completed"
    assert row.metadata == {"source_request_id": "resp-parent", "timestamp_quality": "observed"}
    assert await store.list_usage_records(NOW, attributed_user_id="someone-else") == []


async def test_unknown_service_tier_keeps_tokens_but_is_unpriced(ledger):
    store, make_recorder, _ = ledger
    recorder = make_recorder()
    await recorder.on_event(usage_event(service_tier="priority"))
    [row] = await store.list_usage_records(NOW)
    assert row.cost_usd is None
    assert row.pricing["service_tier_basis"] == "unsupported_unpriced"
    assert recorder.totals() == (row.usage, None)


async def test_recorded_price_snapshot_survives_rate_change_and_replay(ledger):
    store, make_recorder, prices = ledger
    recorder = make_recorder()
    event = usage_event()
    await recorder.on_event(event)
    [original] = await store.list_usage_records(NOW)
    prices.entries[:] = [PriceEntry("*", date(2026, 1, 1), 200, 20, 300, 1000)]
    await make_recorder().on_event(event)
    [saved] = await store.list_usage_records(NOW)
    assert saved.pricing == original.pricing
    assert saved.cost_usd == original.cost_usd
    assert saved.pricing["rates_per_million"]["input_per_mtok"] == 2


async def test_tool_completion_estimates_one_read_without_base64_or_extra_spend(ledger):
    store, make_recorder, _ = ledger
    recorder = make_recorder()
    await recorder.on_event(RunEvent(RunEventKind.TOOL_STARTED,
        {"id": "call-file", "tool": "read_file", "input": {"path": "quote.pdf"},
         "request_id": "resp-parent"}, at=NOW))
    output = [{"type": "text", "text": "Quote total: 123."},
              {"type": "image", "image_url": "data:image/png;base64," + "A"*50_000}]
    await recorder.on_event(RunEvent(RunEventKind.TOOL_FINISHED,
        {"id": "call-file", "tool": "read_file", "output": output, "status": "completed"},
        at=NOW+timedelta(seconds=2)))
    [call] = await store.list_tool_call_usage(NOW, attributed_user_id="user-7")
    assert call.request_id == "resp-parent" and call.tool_name == "read_file"
    assert call.status == "completed" and call.ended_at == NOW+timedelta(seconds=2)
    assert call.input_tokens_estimate > 0
    assert 0 < call.output_tokens_estimate < 100
    assert call.output_images == 1 and call.output_bytes < 200
    assert call.single_read_context_cost_estimate_usd == pytest.approx(call.output_tokens_estimate*2/1e6)
    assert call.estimate_method == "o200k_base_text_only_one_uncached_read"
    # Context estimates are separate from provider costs, not another billable row.
    assert await store.list_usage_records(NOW) == []
    assert recorder.totals() == (TokenUsage(), None)


async def test_provider_usage_is_durable_before_cancellation_without_run_finish(ledger):
    store, make_recorder, _ = ledger
    async def interrupted():
        await make_recorder().on_event(usage_event())
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await interrupted()
    [row] = await store.list_usage_records(NOW)
    assert row.usage == TokenUsage(100, 20, 10, 8, 3)
    assert row.cost_usd is not None
    [run] = await store.list_runs(THREAD)
    assert run.status == RunStatus.RUNNING and run.finished_at is None


async def test_tool_request_link_can_arrive_with_completion(ledger):
    store, make_recorder, _ = ledger
    recorder = make_recorder()
    await recorder.on_event(RunEvent(RunEventKind.TOOL_STARTED,
        {"id": "late-link", "tool": "read_file", "input": {}}, at=NOW))
    await recorder.on_event(RunEvent(RunEventKind.TOOL_FINISHED,
        {"id": "late-link", "tool": "read_file", "request_id": "resp-parent",
         "output": "read", "status": "completed"}, at=NOW+timedelta(seconds=1)))
    call = await store.get_tool_call_usage("run-1", "late-link")
    assert call.request_id == "resp-parent"


async def test_tool_error_flag_survives_steps_ledger_and_transcript(ledger):
    from handler.runtime.run import transcript_message
    store, make_recorder, _ = ledger
    recorder = make_recorder()
    event = RunEvent(RunEventKind.TOOL_FINISHED, {"id": "bad-call", "tool": "gmail_search",
        "status": "completed", "is_error": True, "output": "Disconnected"}, at=NOW)
    await recorder.on_event(event)
    step, = await store.list_steps("run-1")
    assert step.status == "error" and step.data["is_error"]
    usage = await store.get_tool_call_usage("run-1", "bad-call")
    assert usage.status == "error"
    message = transcript_message(THREAD, "run-1", event)
    assert any(b.get("is_error") for b in message.blocks if b["type"] == "tool_result")
