"""Automation aggregates must count full history and never double bill requests."""
from datetime import timedelta
from uuid import uuid4

import pytest

from handler.runtime.observability.ledger import UsageRecord
from handler.runtime.types import Event, Lane, Run, RunStatus, ThreadSpec, ThreadType, TokenUsage


async def add_run(store, clock, key, *, kind=ThreadType.AUTOMATION, status=RunStatus.COMPLETED, cost=1.0):
    event = Event(type="automation.fired", source="test", payload={}, idempotency_key=str(uuid4()), thread_key=key, received_at=clock())
    await store.put(event, ThreadSpec(key, kind, Lane.BACKGROUND, harness="openai_agents", model="test-model"))
    lease, = await store.claim("w", Lane.BACKGROUND, 1, 60)
    run = Run("r_" + event.id, key, lease.version, [event.id], RunStatus.RUNNING,
              "openai_agents", "test-model", started_at=clock())
    await store.create_run(lease, run)
    if status != RunStatus.RUNNING:
        run.status = status
        run.finished_at = clock()
        run.usage = TokenUsage(input_tokens=100, output_tokens=10)
        run.cost_usd = cost
        await store.finish_run(lease, run)
        await store.complete(lease, run.id, [event.id])
    return run


async def test_cost_legacy_fallback_statuses_and_exclude_chat(domain_store, clock):
    store = domain_store
    old = await add_run(store, clock, "automation:one", cost=1)
    await store.add_usage_record(UsageRecord("req-old", old.id, old.thread_key, cost_usd=1, occurred_at=clock()))
    clock.advance(60)
    unknown = await add_run(store, clock, "automation:one", status=RunStatus.FAILED, cost=None)
    await store.add_usage_record(UsageRecord("req-part-priced", unknown.id, unknown.thread_key, cost_usd=.2, occurred_at=clock()))
    await store.add_usage_record(UsageRecord("req-part-unknown", unknown.id, unknown.thread_key, cost_usd=None, occurred_at=clock()))
    await add_run(store, clock, "chat:user:one", kind=ThreadType.CHAT, cost=999)
    live = await add_run(store, clock, "automation:two", status=RunStatus.RUNNING)
    await store.add_usage_record(UsageRecord("req-live", live.id, live.thread_key, cost_usd=.3,
        usage=TokenUsage(input_tokens=40, output_tokens=5), occurred_at=clock()))
    rows = {row["thread_key"]: row for row in await store.automation_metrics()}
    assert set(rows) == {"automation:one", "automation:two"}
    one = rows["automation:one"]
    assert one["runs"] == 2 and one["completed"] == one["failed"] == 1
    assert one["known_cost_usd"] == pytest.approx(1.2)  # 1, NOT 1 + duplicated request 1
    assert one["recorded_model_requests"] == 3
    assert one["unpriced_runs"] == 1
    assert one["input_tokens"] == 200
    assert one["last_run_status"] == "failed"
    two = rows["automation:two"]
    assert two["known_cost_usd"] == pytest.approx(.3)
    assert two["running"] == 1 and two["unpriced_runs"] == 0
    assert two["input_tokens"] == 40
    recent = {row["thread_key"]: row for row in await store.automation_metrics(clock() - timedelta(seconds=1))}
    assert recent["automation:one"]["runs"] == 1
    assert recent["automation:one"]["known_cost_usd"] == pytest.approx(.2)


async def test_no_artificial_run_cap_and_zero_priced_runs(domain_store, clock):
    store = domain_store
    # Raw bulk insertion exercises aggregates beyond the old 5,000-run scan cap.
    first = await add_run(store, clock, "automation:many", cost=0)
    count = 5001
    if getattr(store, '_usage_pg', False):
        query = """INSERT INTO runs(id,thread_key,lease_version,trigger_event_ids,status,harness,model,started_at,finished_at,usage,cost_usd)
            SELECT 'bulk-' || n,thread_key,lease_version,trigger_event_ids,status,harness,model,started_at,finished_at,usage,cost_usd
            FROM runs CROSS JOIN generate_series(1, :count) n WHERE id=:id"""
    else:
        query = """WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM nums WHERE n<:count)
            INSERT INTO runs(id,thread_key,lease_version,trigger_event_ids,status,harness,model,started_at,finished_at,usage,cost_usd)
            SELECT 'bulk-' || n,thread_key,lease_version,trigger_event_ids,status,harness,model,started_at,finished_at,usage,cost_usd
            FROM runs CROSS JOIN nums WHERE id=:id"""
    await store._usage_query(query, {"count": count, "id": first.id})
    rows = await store.automation_metrics()
    assert rows[0]["runs"] == count + 1
    assert rows[0]["known_cost_usd"] == 0 and rows[0]["unpriced_runs"] == 0
