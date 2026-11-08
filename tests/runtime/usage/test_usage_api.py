from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from handler.runtime.auth import Authenticator
from handler.runtime.observability.ledger import RunAttribution, ToolCallUsage, UsageRecord
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.store.sqlite_users import User
from handler.runtime.types import TokenUsage
from handler.runtime.web.usage_api import usage_router

STAMP = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


class LedgerStore:
    """Contract fake: report tests stay independent of the database migration."""
    def __init__(self):
        self.rows = [UsageRecord(str(i), "r", "chat:alice:t", model="sol", occurred_at=STAMP,
                     usage=TokenUsage(input_tokens=10), cost_usd=0.01,
                     metadata={"prompt": "SECRET"}) for i in range(501)]
        self.rows += [UsageRecord("helper", "r", "chat:alice:t", tool_call_id="c", tool_name="helper",
                      model="luna", occurred_at=STAMP, usage=TokenUsage(input_tokens=20), cost_usd=0.5),
                      UsageRecord("unpriced", "s", "mail:m:t", model="unknown", occurred_at=STAMP)]
        self.attrs = [RunAttribution("r", "chat:alice:t", attributed_user_id="alice", trigger_type="chat"),
                      RunAttribution("s", "mail:m:t", trigger_type="mail")]
        self.calls = [ToolCallUsage("tc", "r", "chat:alice:t", "c", "helper", started_at=STAMP,
                     output_tokens_estimate=42, single_read_context_cost_estimate_usd=0.02)]

    async def list_run_attributions(self, run_ids):
        return [a for a in self.attrs if a.run_id in run_ids]

    def select(self, rows, filters, stamp):
        attrs = {a.run_id: a for a in self.attrs}
        return [r for r in rows if filters["since"] <= getattr(r, stamp) < filters["until"] and
                all(value is None or (getattr(attrs[r.run_id], "attributed_user_id") if key == "attributed_user_id"
                                     else getattr(r, key)) == value
                    for key, value in filters.items() if key not in {"since", "until"})]

    async def list_usage_records(self, limit, offset, **filters):
        return self.select(self.rows, filters, "occurred_at")[offset:offset + limit]

    async def list_tool_call_usage(self, limit, offset, **filters):
        return self.select(self.calls, filters, "started_at")[offset:offset + limit]


@pytest.fixture
async def report(tmp_path):
    users = SQLiteStore(tmp_path / "auth.db")
    await users.create_user(User("alice", "Alice", "member"))
    _, token = await users.create_token("alice", "test")
    store = LedgerStore()
    app = FastAPI()
    app.include_router(usage_router(store, Authenticator(users, "admin-secret")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        yield client, store, token
    await users.close()


BASE = "since=2026-11-08&until=2026-11-09"
ADMIN = {"Authorization": "Bearer admin-secret"}


@pytest.mark.parametrize("path", ["records", "breakdown", "tools", "tool-calls"])
async def test_admin_only(report, path):
    client, _, token = report
    url = f"/api/usage/{path}?{BASE}"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers={"Authorization": f"Bearer {token}"})).status_code == 403
    assert (await client.get(url, headers={**ADMIN, "X-Handler-User": "alice"})).status_code == 403


async def test_pages_filters_and_no_payloads(report):
    client, _, _ = report
    first = (await client.get(f"/api/usage/records?{BASE}&limit=2", headers=ADMIN)).json()
    assert len(first["records"]) == 2 and first["next_offset"] == 2
    assert "SECRET" not in str(first)
    assert "hosted_tool_fees" in first["cost_basis"]
    assert first["records"][0]["attribution"]["attributed_user_id"] == "alice"
    last = (await client.get(f"/api/usage/records?{BASE}&offset=502&limit=2", headers=ADMIN)).json()
    assert len(last["records"]) == 1 and last["next_offset"] is None
    filtered = (await client.get(f"/api/usage/records?{BASE}&user_id=alice&model=luna&tool_name=helper", headers=ADMIN)).json()
    assert [r["id"] for r in filtered["records"]] == ["helper"]
    empty = (await client.get(f"/api/usage/records?{BASE}&user_id=bob", headers=ADMIN)).json()
    assert empty["records"] == []


async def test_aggregate_full_scan_and_cross_dimension_totals(report):
    client, _, _ = report
    for dims in ["user", "thread", "tool", "model", "date", "trigger", "user,thread,tool,model,date,trigger"]:
        data = (await client.get(f"/api/usage/breakdown?{BASE}&group_by={dims}", headers=ADMIN)).json()
        assert data["totals"]["records"] == 503
        assert data["totals"]["known_cost_usd"] == pytest.approx(5.51)
        assert sum(g["known_cost_usd"] for g in data["groups"]) == pytest.approx(5.51)
        assert data["totals"]["unpriced_records"] == 1
        assert data["totals"]["pricing_complete"] is False
        assert data["coverage"]["historical_run_totals_included"] is False
    assert (await client.get(f"/api/usage/breakdown?{BASE}&group_by=prompt", headers=ADMIN)).status_code == 422
    assert (await client.get(f"/api/usage/breakdown?{BASE}&group_by=user,user", headers=ADMIN)).status_code == 422


async def test_tool_direct_cost_and_context_estimates_remain_separate(report):
    client, _, _ = report
    data = (await client.get(f"/api/usage/tools?{BASE}", headers=ADMIN)).json()
    helper = next(g for g in data["groups"] if g["tool"] == "helper")
    assert helper["known_cost_usd"] == 0.5
    assert helper["single_read_context_cost_estimate_usd"] == 0.02
    assert helper["calls"] == 1 and helper["output_tokens_estimate"] == 42
    assert data["coverage"]["tool_calls"] == 1
    assert "hosted_tool_fees" in data["cost_basis"]
    assert sum(g["known_cost_usd"] for g in data["groups"]) == pytest.approx(5.51)
    filtered = (await client.get(f"/api/usage/tools?{BASE}&model=sol", headers=ADMIN)).json()
    assert all(g["calls"] == 0 for g in filtered["groups"])
    filtered = (await client.get(f"/api/usage/tools?{BASE}&model=luna", headers=ADMIN)).json()
    assert filtered["groups"][0]["calls"] == 1


@pytest.mark.parametrize("query", ["since=bad", "since=2026-11-10&until=2026-11-08", "since=2000-01-01&until=2026-11-09", "days=0", "limit=0", "offset=-1"])
async def test_invalid_windows_and_pagination(report, query):
    client, _, _ = report
    assert (await client.get(f"/api/usage/records?{query}", headers=ADMIN)).status_code == 422


async def test_reports_are_bound_to_instance_store(tmp_path):
    stores = [SQLiteStore(tmp_path / f"{name}.db") for name in ("main", "bss")]
    try:
        for i, store in enumerate(stores):
            await store.save_run_attribution(RunAttribution("same-run", "chat:alice:t", instance_name=str(i), attributed_user_id="alice"))
            await store.add_usage_record(UsageRecord("same-record", "same-run", "chat:alice:t", occurred_at=STAMP, cost_usd=float(i + 1)))
        app = FastAPI()
        for name, store in zip(("main", "bss"), stores):
            app.include_router(usage_router(store, Authenticator(store, f"key-{name}")), prefix=f"/{name}")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            for i, name in enumerate(("main", "bss")):
                result = await client.get(f"/{name}/api/usage/breakdown?{BASE}&user_id=alice", headers={"Authorization": f"Bearer key-{name}"})
                assert result.status_code == 200
                assert result.json()["totals"]["known_cost_usd"] == float(i + 1)
            assert (await client.get(f"/bss/api/usage/records?{BASE}", headers={"Authorization": "Bearer key-main"})).status_code == 401
    finally:
        for store in stores:
            await store.close()


async def test_individual_tool_calls_filters_pagination_and_attribution(report):
    client, store, _ = report
    store.calls[0].metadata = {"raw_output": "SECRET"}
    store.calls.append(ToolCallUsage("tc2", "s", "mail:m:t", "c2", "mail_read", started_at=STAMP))
    first = (await client.get(f"/api/usage/tool-calls?{BASE}&limit=1", headers=ADMIN)).json()
    assert first["next_offset"] == 1
    assert "SECRET" not in str(first) and "metadata" not in first["tool_calls"][0]
    call = first["tool_calls"][0]
    assert call["run_id"] == "r" and call["tool_call_id"] == "c"
    assert call["attribution"]["attributed_user_id"] == "alice"
    assert call["output_tokens_estimate"] == 42
    assert call["single_read_context_cost_estimate_usd"] == 0.02
    assert "NONADDITIVE" in first["estimate_basis"]
    last = (await client.get(f"/api/usage/tool-calls?{BASE}&limit=1&offset=1", headers=ADMIN)).json()
    assert last["next_offset"] is None
    assert last["tool_calls"][0]["tool_call_id"] == "c2"
    for filters in ["user_id=alice", "thread_key=chat:alice:t", "tool_name=helper", "run_id=r"]:
        result = (await client.get(f"/api/usage/tool-calls?{BASE}&{filters}", headers=ADMIN)).json()
        assert [r["tool_call_id"] for r in result["tool_calls"]] == ["c"]
    empty = (await client.get("/api/usage/tool-calls?since=2026-11-09&until=2026-11-10", headers=ADMIN)).json()
    assert empty["tool_calls"] == []
    for invalid in ["limit=0", "offset=-1", "days=0", "since=bad"]:
        assert (await client.get(f"/api/usage/tool-calls?{invalid}", headers=ADMIN)).status_code == 422
