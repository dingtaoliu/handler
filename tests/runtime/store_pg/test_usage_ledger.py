"""Ledger invariants run against both real Postgres and SQLite."""
from dataclasses import replace
from datetime import timedelta

import pytest

from handler.runtime.observability.ledger import RunAttribution, ToolCallUsage, UsageRecord
from handler.runtime.store.sqlite import SQLiteCore
from handler.runtime.types import TokenUsage


async def test_attribution_and_usage_first_write_wins(store, clock):
    original = RunAttribution("r", "thread", attributed_user_id="u", captured_at=clock(), trigger_event_ids=["e"])
    await store.save_run_attribution(original)
    await store.save_run_attribution(replace(original, attributed_user_id="someone_else"))
    assert await store.get_run_attribution("r") == original
    assert await store.get_run_attribution("missing") is None
    assert await store.list_run_attributions([]) == []
    assert await store.list_run_attributions(["r", "absent"]) == [original]
    record = UsageRecord("req", "r", "thread", occurred_at=clock(), usage=TokenUsage(input_tokens=123, cached_input_tokens=23), cost_usd=.01, metadata={"nested": ["中\x00文"]})
    assert await store.add_usage_record(record)
    assert not await store.add_usage_record(replace(record, cost_usd=999))
    rows = await store.list_usage_records(clock(), attributed_user_id="u")
    assert len(rows) == 1
    assert rows[0].usage == record.usage
    assert rows[0].cost_usd == .01
    assert rows[0].metadata == {"nested": ["中文"]}
    assert not await store.list_usage_records(clock(), attributed_user_id="someone_else")


async def test_date_filters_pagination_and_model_tool_dimensions(store, clock):
    start = clock()
    await store.save_run_attribution(RunAttribution("r", "t", attributed_user_id="u"))
    for ident, run, thread, model, tool, date in [
        ("b", "r", "t", "sol", "mail_read", start),
        ("a", "r", "t", "luna", "classify", start),
        ("c", "r2", "t2", "sol", None, start + timedelta(days=1)),
    ]:
        await store.add_usage_record(UsageRecord(ident, run, thread, model=model, tool_name=tool, occurred_at=date))
    assert [r.id for r in await store.list_usage_records(start)] == ["a", "b", "c"]
    assert [r.id for r in await store.list_usage_records(start, start + timedelta(days=1))] == ["a", "b"]
    assert [r.id for r in await store.list_usage_records(start, limit=1, offset=1)] == ["b"]
    assert [r.id for r in await store.list_usage_records(start, model="sol", tool_name="mail_read", thread_key="t", run_id="r", attributed_user_id="u")] == ["b"]
    assert [r.id for r in await store.list_usage_records(start + timedelta(seconds=1))] == ["c"]


async def test_tool_upsert_preserves_identity_and_completion(store, clock):
    await store.save_run_attribution(RunAttribution("r", "t", attributed_user_id="u"))
    call = ToolCallUsage("toolrow", "r", "t", "call", "mail_read", started_at=clock(), metadata={"first": 1})
    await store.upsert_tool_call_usage(call)
    done = replace(call, ended_at=clock() + timedelta(seconds=2), status="completed", request_id="req", input_tokens_estimate=12, output_tokens_estimate=55, output_bytes=100, output_images=1, single_read_context_cost_estimate_usd=.001, metadata={"finish": 2})
    await store.upsert_tool_call_usage(done)
    # A delayed/replayed start must not erase recorded usage or regress status.
    await store.upsert_tool_call_usage(replace(call, id="other", thread_key="other", tool_name="other"))
    saved = await store.get_tool_call_usage("r", "call")
    assert saved.id == "toolrow"
    assert saved.thread_key == "t"
    assert saved.tool_name == "mail_read"
    assert saved.status == "completed"
    assert saved.ended_at == done.ended_at
    assert saved.request_id == "req"
    assert saved.input_tokens_estimate == 12
    assert saved.output_tokens_estimate == 55
    assert saved.output_bytes == 100
    assert saved.output_images == 1
    assert saved.single_read_context_cost_estimate_usd == .001
    assert saved.metadata == {"first": 1, "finish": 2}
    assert await store.get_tool_call_usage("missing", "call") is None
    assert [r.id for r in await store.list_tool_call_usage(clock(), thread_key="t", attributed_user_id="u", tool_name="mail_read", run_id="r")] == ["toolrow"]
    assert not await store.list_tool_call_usage(clock(), until=clock())
    assert not await store.list_tool_call_usage(clock(), attributed_user_id="other")
    assert not await store.list_tool_call_usage(clock(), offset=1)


async def test_sqlite_reopen_applies_schema_idempotently_without_backfill(tmp_path, clock):
    path = tmp_path / "persist.db"
    first = SQLiteCore(path)
    assert not await first.list_usage_records(clock())
    original = UsageRecord("req", "run", "thread", occurred_at=clock())
    await first.add_usage_record(original)
    await first.close()
    second = SQLiteCore(path)
    try:
        assert await second.list_usage_records(clock()) == [original]
        assert not await second.add_usage_record(original)
    finally:
        await second.close()


async def test_postgres_reopen_and_schema_isolation(pg_dsn, clock):
    from handler.runtime.store.postgres import PostgresCore
    original = UsageRecord("req", "run", "thread", occurred_at=clock())
    first = PostgresCore(pg_dsn, schema="tenant_one")
    await first.add_usage_record(original)
    await first.close()
    second = PostgresCore(pg_dsn, schema="tenant_one")
    other = PostgresCore(pg_dsn, schema="tenant_two")
    try:
        assert await second.list_usage_records(clock()) == [original]
        assert not await other.list_usage_records(clock())
    finally:
        await second.close()
        await other.close()
