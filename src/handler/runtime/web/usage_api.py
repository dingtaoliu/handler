"""Admin-only, additive usage-ledger reports. No prompt or tool-body exports."""
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import Authenticator
from ..types import TokenUsage, utcnow

DIMENSIONS = {"user", "thread", "tool", "model", "date", "trigger"}
PAGE_SIZE = 500
COST_BASIS = "estimated_model_token_cost_only; hosted_tool_fees_and_external_service_charges_excluded"


def _window(since, until, days):
    def parse(value):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
        except (ValueError, AttributeError):
            raise HTTPException(422, "Dates must be ISO-8601 dates or timestamps")
    end = parse(until) if until else utcnow()
    start = parse(since) if since else end - timedelta(days=days)
    if start >= end or end - start > timedelta(days=3650):
        raise HTTPException(422, "Window must be positive and at most 3650 days; until is exclusive")
    return start, end


async def _scan(method, filters):
    """Fetch every page rather than silently truncating aggregate totals."""
    result, offset = [], 0
    while True:
        page = await method(**filters, limit=PAGE_SIZE, offset=offset)
        result.extend(page)
        if len(page) < PAGE_SIZE:
            return result
        offset += len(page)


async def _attributions(store, records):
    ids = sorted({r.run_id for r in records})
    result = {}
    for offset in range(0, len(ids), PAGE_SIZE):
        result.update({r.run_id: r for r in await store.list_run_attributions(ids[offset:offset + PAGE_SIZE])})
    return result


def _coverage(records):
    return {"basis": "ledger_only_no_historical_backfill", "records": len(records),
            "first_record_in_window": min((r.occurred_at for r in records), default=None),
            "historical_run_totals_included": False}


def _public(record, attribution):
    # Metadata is deliberately excluded: neither future raw payloads nor excerpts
    # should accidentally become visible through reporting.
    value = {k: v for k, v in asdict(record).items() if k not in {"metadata"}}
    value["timestamp_quality"] = record.metadata.get("timestamp_quality", "unknown")
    value["source_request_id"] = record.metadata.get("source_request_id")
    value["attribution"] = asdict(attribution) if attribution else None
    return value


def _aggregate(records, dimensions, attributions):
    def bucket():
        return {"records": 0, "priced_records": 0, "unpriced_records": 0,
                "known_cost_usd": 0.0, "usage": TokenUsage(), "runs": set()}
    buckets, total = defaultdict(bucket), bucket()
    for r in records:
        a = attributions.get(r.run_id)
        labels = {"user": a.attributed_user_id if a and a.attributed_user_id else ("__shared_system__" if a else "__unknown__"),
                  "thread": r.thread_key, "tool": r.tool_name or "__agent__",
                  "model": r.model or "__unknown__", "date": r.occurred_at.astimezone(timezone.utc).date().isoformat(),
                  "trigger": a.trigger_type if a else "__unknown__"}
        key = tuple(labels[d] for d in dimensions)
        for b in (buckets[key], total):
            b["records"] += 1
            b["runs"].add(r.run_id)
            b["usage"] += r.usage
            if r.cost_usd is None:
                b["unpriced_records"] += 1
            else:
                b["priced_records"] += 1
                b["known_cost_usd"] += r.cost_usd
    def render(b):
        return {**asdict(b["usage"]), **{k: v for k, v in b.items() if k not in {"usage", "runs"}},
                "runs": len(b["runs"]), "cost_usd": b["known_cost_usd"] if b["priced_records"] else None,
                "pricing_complete": b["unpriced_records"] == 0}
    return render(total), [{**dict(zip(dimensions, key)), **render(b)} for key, b in sorted(buckets.items())]


def usage_router(store, auth: Authenticator) -> APIRouter:
    router = APIRouter(dependencies=[Depends(auth.require_admin)])

    def filters(since, until, days, thread_key, user_id, model, tool_name, run_id):
        start, end = _window(since, until, days)
        return dict(since=start, until=end, thread_key=thread_key,
                    attributed_user_id=user_id, model=model, tool_name=tool_name, run_id=run_id)

    @router.get("/api/usage/records")
    async def records(since: str | None = None, until: str | None = None,
                      days: int = Query(30, ge=1, le=3650), thread_key: str | None = None,
                      user_id: str | None = None, model: str | None = None,
                      tool_name: str | None = None, run_id: str | None = None,
                      limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)):
        f = filters(since, until, days, thread_key, user_id, model, tool_name, run_id)
        rows = await store.list_usage_records(**f, limit=limit + 1, offset=offset)
        more, rows = len(rows) > limit, rows[:limit]
        attrs = await _attributions(store, rows)
        return {"records": [_public(r, attrs.get(r.run_id)) for r in rows],
                "next_offset": offset + limit if more else None, "coverage": _coverage(rows),
                "since": f["since"], "until": f["until"], "cost_basis": COST_BASIS}

    @router.get("/api/usage/tool-calls")
    async def tool_calls(since: str | None = None, until: str | None = None,
                         days: int = Query(30, ge=1, le=3650), thread_key: str | None = None,
                         user_id: str | None = None, tool_name: str | None = None,
                         run_id: str | None = None, limit: int = Query(200, ge=1, le=1000),
                         offset: int = Query(0, ge=0)):
        f = filters(since, until, days, thread_key, user_id, None, tool_name, run_id)
        del f["model"]
        rows = await store.list_tool_call_usage(**f, limit=limit + 1, offset=offset)
        more, rows = len(rows) > limit, rows[:limit]
        attrs = await _attributions(store, rows)
        result = []
        for row in rows:
            value = {k: v for k, v in asdict(row).items() if k != "metadata"}
            value["attribution"] = asdict(attrs[row.run_id]) if row.run_id in attrs else None
            result.append(value)
        return {"tool_calls": result, "next_offset": offset + limit if more else None,
                "since": f["since"], "until": f["until"], "cost_basis": COST_BASIS,
                "coverage": {"basis": "ledger_only_no_historical_backfill", "tool_calls": len(rows),
                             "historical_run_totals_included": False},
                "estimate_basis": "text only, hypothetical one uncached read; NONADDITIVE to model token cost; excludes repeated history and images"}

    @router.get("/api/usage/breakdown")
    async def breakdown(group_by: str = "user", since: str | None = None, until: str | None = None,
                        days: int = Query(30, ge=1, le=3650), thread_key: str | None = None,
                        user_id: str | None = None, model: str | None = None,
                        tool_name: str | None = None, run_id: str | None = None):
        dims = [d.strip() for d in group_by.split(",")]
        if not dims or len(dims) != len(set(dims)) or not set(dims) <= DIMENSIONS:
            raise HTTPException(422, "group_by must contain unique dimensions: user,thread,tool,model,date,trigger")
        f = filters(since, until, days, thread_key, user_id, model, tool_name, run_id)
        rows = await _scan(store.list_usage_records, f)
        total, groups = _aggregate(rows, dims, await _attributions(store, rows))
        return {"group_by": dims, "totals": total, "groups": groups, "coverage": _coverage(rows),
                "since": f["since"], "until": f["until"], "date_timezone": "UTC",
                "cost_basis": COST_BASIS,
                "allocation_basis": "direct_ledger_cost_once; tool-free agent requests use __agent__"}

    @router.get("/api/usage/tools")
    async def tools(since: str | None = None, until: str | None = None,
                    days: int = Query(30, ge=1, le=3650), thread_key: str | None = None,
                    user_id: str | None = None, model: str | None = None,
                    tool_name: str | None = None, run_id: str | None = None):
        f = filters(since, until, days, thread_key, user_id, model, tool_name, run_id)
        ledger = await _scan(store.list_usage_records, f)
        tool_filters = {k: v for k, v in f.items() if k != "model"}
        calls = await _scan(store.list_tool_call_usage, tool_filters)
        # A model filter selects calls with a directly linked usage record, never
        # guesses a tool's model from the enclosing run's default.
        if model:
            linked = {(r.run_id, r.tool_call_id) for r in ledger if r.tool_call_id}
            calls = [c for c in calls if (c.run_id, c.tool_call_id) in linked]
        _, direct = _aggregate(ledger, ["tool"], await _attributions(store, ledger))
        groups = {r["tool"]: {**r, "calls": 0, "errors": 0, "input_tokens_estimate": 0,
                  "output_tokens_estimate": 0, "output_bytes": 0, "output_images": 0,
                  "single_read_context_cost_estimate_usd": 0.0, "priced_estimate_calls": 0,
                  "estimated_calls": 0} for r in direct}
        for c in calls:
            if c.tool_name not in groups:
                empty, _ = _aggregate([], [], {})
                groups[c.tool_name] = {**empty, "tool": c.tool_name, "calls": 0, "errors": 0,
                    "input_tokens_estimate": 0, "output_tokens_estimate": 0, "output_bytes": 0,
                    "output_images": 0, "single_read_context_cost_estimate_usd": 0.0,
                    "priced_estimate_calls": 0, "estimated_calls": 0}
            b = groups[c.tool_name]
            b["calls"] += 1
            b["errors"] += c.status in {"failed", "error"}
            for k in ("input_tokens_estimate", "output_tokens_estimate", "output_bytes", "output_images"):
                b[k] += getattr(c, k) or 0
            b["estimated_calls"] += c.output_tokens_estimate is not None
            if c.single_read_context_cost_estimate_usd is not None:
                b["single_read_context_cost_estimate_usd"] += c.single_read_context_cost_estimate_usd
                b["priced_estimate_calls"] += 1
        for b in groups.values():
            if not b["priced_estimate_calls"]:
                b["single_read_context_cost_estimate_usd"] = None
        return {"groups": sorted(groups.values(), key=lambda r: (-r["calls"], r["tool"])),
                "coverage": {**_coverage(ledger), "tool_calls": len(calls),
                             "first_tool_call_in_window": min((c.started_at for c in calls), default=None)},
                "since": f["since"], "until": f["until"], "cost_basis": COST_BASIS,
                "estimate_basis": "text only, hypothetical one uncached read; NONADDITIVE to provider cost; excludes repeated history and images",
                "model_filter_basis": "directly_linked_usage_records_only" if model else "all_recorded_calls"}

    return router
