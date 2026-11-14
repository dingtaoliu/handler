"""Durable cost ledger shared by SQLite and Postgres.

Provider charges are immutable rows; text-context estimates live separately on
calls and must never be summed into billed spend. No prompts or tool bodies are
stored here. Attribution snapshots intentionally survive user/thread changes.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, fields
from datetime import datetime, timezone
from typing import Any

from ..observability.ledger import RunAttribution, ToolCallUsage, UsageRecord
from ..types import TokenUsage

_JSON = {"trigger_event_ids", "usage", "pricing", "metadata"}
_TIMES = {"captured_at", "started_at", "ended_at", "occurred_at"}


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _decode(cls, row):
    values = {f.name: row[f.name] for f in fields(cls)}
    for name, value in values.items():
        if name in _JSON and isinstance(value, str):
            values[name] = json.loads(value)
        elif name in _TIMES and isinstance(value, str):
            values[name] = datetime.fromisoformat(value)
    if "usage" in values:
        values["usage"] = TokenUsage(**values["usage"])
    return cls(**values)


class UsageMixin:
    _usage_pg = False

    def _usage_values(self, record):
        values = _clean(asdict(record))
        for key, value in values.items():
            if key in _JSON:
                if self._usage_pg:
                    from psycopg.types.json import Jsonb
                    values[key] = Jsonb(value)
                else:
                    values[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            elif isinstance(value, datetime) and not self._usage_pg:
                values[key] = value.astimezone(timezone.utc).isoformat(timespec="microseconds")
        return values

    async def _usage_query(self, query, parameters, *, fetch=False):
        if self._usage_pg:
            query = re.sub(r":([a-z_][a-z_0-9]*)", r"%(\1)s", query)
            async with self._conn() as conn:
                cursor = await conn.execute(query, parameters)
                return await cursor.fetchall() if fetch else cursor.rowcount
        def execute(db):
            cursor = db.execute(query, parameters)
            return cursor.fetchall() if fetch else cursor.rowcount
        return await self._call(execute)

    async def _usage_insert(self, table, record, conflict):
        values = self._usage_values(record)
        query = (f"INSERT INTO {table} ({', '.join(values)}) VALUES "
                 f"({', '.join(':' + key for key in values)}) {conflict}")
        return await self._usage_query(query, values)

    async def save_run_attribution(self, attribution: RunAttribution) -> None:
        await self._usage_insert("run_attributions", attribution, "ON CONFLICT (run_id) DO NOTHING")

    async def get_run_attribution(self, run_id: str) -> RunAttribution | None:
        rows = await self._usage_query("SELECT * FROM run_attributions WHERE run_id=:run_id", {"run_id": run_id}, fetch=True)
        return _decode(RunAttribution, rows[0]) if rows else None

    async def list_run_attributions(self, run_ids: list[str]) -> list[RunAttribution]:
        result = []
        # Bound the parameter count for SQLite and large reporting windows.
        for start in range(0, len(run_ids), 400):
            parameters = {f"r{i}": rid for i, rid in enumerate(run_ids[start:start + 400])}
            rows = await self._usage_query(
                "SELECT * FROM run_attributions WHERE run_id IN (" + ",".join(":" + k for k in parameters) + ") ORDER BY run_id",
                parameters, fetch=True)
            result.extend(_decode(RunAttribution, row) for row in rows)
        return result

    async def add_usage_record(self, record: UsageRecord) -> bool:
        return bool(await self._usage_insert("usage_records", record, "ON CONFLICT (id) DO NOTHING"))

    async def upsert_tool_call_usage(self, call: ToolCallUsage) -> None:
        table = "tool_call_usage"
        updates = [f"{name}=COALESCE(excluded.{name}, {table}.{name})" for name in (
            "ended_at", "input_tokens_estimate", "output_tokens_estimate",
            "single_read_context_cost_estimate_usd")]
        updates.append(f"request_id=COALESCE({table}.request_id, excluded.request_id)")
        greatest = "GREATEST" if self._usage_pg else "MAX"
        updates.extend(f"{name}={greatest}(excluded.{name}, {table}.{name})" for name in ("output_bytes", "output_images"))
        updates.append(f"status=CASE WHEN excluded.status='running' AND {table}.status<>'running' THEN {table}.status ELSE excluded.status END")
        updates.append("metadata=" + (f"{table}.metadata || excluded.metadata" if self._usage_pg else f"json_patch({table}.metadata, excluded.metadata)"))
        await self._usage_insert(table, call, "ON CONFLICT (run_id, tool_call_id) DO UPDATE SET " + ", ".join(updates))

    async def get_tool_call_usage(self, run_id: str, tool_call_id: str) -> ToolCallUsage | None:
        rows = await self._usage_query("SELECT * FROM tool_call_usage WHERE run_id=:run_id AND tool_call_id=:tool_call_id", {"run_id": run_id, "tool_call_id": tool_call_id}, fetch=True)
        return _decode(ToolCallUsage, rows[0]) if rows else None

    async def automation_metrics(self, since: datetime | None = None) -> list[dict[str, Any]]:
        """Aggregate every recorded run in SQL; never cap/scan a thread history.

        Finished run totals include legacy usage. Only fall back to request-ledger
        costs when a run has no total; adding both would double count spending.
        """
        params = {}
        where = "t.type='automation'"
        if since is not None:
            where += " AND r.started_at>=:since"
            params["since"] = since if self._usage_pg else since.astimezone(timezone.utc).isoformat(timespec="microseconds")

        def tokens(alias, key):
            value = f"{alias}.usage->>'{key}'" if self._usage_pg else f"json_extract({alias}.usage, '$.{key}')"
            return f"CAST(COALESCE({value}, '0') AS BIGINT)"

        query = f"""
        WITH selected_runs AS (
            SELECT r.*, t.harness AS thread_harness, t.model AS thread_model
            FROM runs r JOIN threads t ON t.key=r.thread_key WHERE {where}
        ), requests AS (
            SELECT u.run_id, COUNT(*) AS requests, SUM(u.cost_usd) AS cost,
                   SUM(CASE WHEN u.cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced,
                   SUM({tokens('u', 'input_tokens')}) AS input_tokens,
                   SUM({tokens('u', 'output_tokens')}) AS output_tokens
            FROM usage_records u JOIN selected_runs r ON r.id=u.run_id
            WHERE u.record_kind='model_request' GROUP BY u.run_id
        ), ranked AS (
            SELECT r.*, ROW_NUMBER() OVER (PARTITION BY thread_key ORDER BY started_at DESC, id DESC) AS position
            FROM selected_runs r
        ), totals AS (
            SELECT r.thread_key, COUNT(*) AS runs,
                SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN r.status IN ('failed','abandoned') THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN r.status='running' THEN 1 ELSE 0 END) AS running,
                SUM(CASE WHEN r.status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
                SUM(COALESCE(r.cost_usd, q.cost, 0)) AS known_cost_usd,
                SUM(CASE WHEN r.cost_usd IS NULL AND (COALESCE(q.requests,0)=0 OR q.unpriced>0) THEN 1 ELSE 0 END) AS unpriced_runs,
                SUM(COALESCE(q.requests,0)) AS recorded_model_requests,
                SUM(CASE WHEN r.status='running' THEN COALESCE(q.input_tokens,0) ELSE {tokens('r', 'input_tokens')} END) AS input_tokens,
                SUM(CASE WHEN r.status='running' THEN COALESCE(q.output_tokens,0) ELSE {tokens('r', 'output_tokens')} END) AS output_tokens,
                MIN(r.started_at) AS first_run_at, MAX(r.started_at) AS last_run_at
            FROM selected_runs r LEFT JOIN requests q ON q.run_id=r.id GROUP BY r.thread_key
        )
        SELECT a.*, r.status AS last_run_status, r.model AS last_run_model,
               r.finished_at AS last_finished_at, r.thread_harness, r.thread_model
        FROM totals a JOIN ranked r ON a.thread_key=r.thread_key AND r.position=1
        ORDER BY a.known_cost_usd DESC, a.thread_key
        """
        rows = await self._usage_query(query, params, fetch=True)
        return [dict(row) for row in rows]

    async def _usage_list(self, table, cls, since, until, thread_key, attributed_user_id, model, tool_name, run_id, limit, offset):
        date = "started_at" if cls is ToolCallUsage else "occurred_at"
        def time_value(value):
            return value if self._usage_pg else value.astimezone(timezone.utc).isoformat(timespec="microseconds")
        clauses = [f"u.{date}>=:since"]
        parameters = {"since": time_value(since), "limit": max(0, limit), "offset": max(0, offset)}
        if until is not None:
            clauses.append(f"u.{date}<:until")
            parameters["until"] = time_value(until)
        for key, value in (("thread_key", thread_key), ("model", model), ("tool_name", tool_name), ("run_id", run_id)):
            if value is not None:
                clauses.append(f"u.{key}=:{key}")
                parameters[key] = value
        join = ""
        if attributed_user_id is not None:
            join = " JOIN run_attributions a ON a.run_id=u.run_id"
            clauses.append("a.attributed_user_id=:attributed_user_id")
            parameters["attributed_user_id"] = attributed_user_id
        rows = await self._usage_query(f"SELECT u.* FROM {table} u{join} WHERE " + " AND ".join(clauses) + f" ORDER BY u.{date}, u.id LIMIT :limit OFFSET :offset", parameters, fetch=True)
        return [_decode(cls, row) for row in rows]

    async def list_usage_records(self, since: datetime, until: datetime | None = None, *, thread_key: str | None = None, attributed_user_id: str | None = None, model: str | None = None, tool_name: str | None = None, run_id: str | None = None, limit: int = 200, offset: int = 0) -> list[UsageRecord]:
        """Chronological rows, since inclusive and until exclusive; no fabricated history."""
        return await self._usage_list("usage_records", UsageRecord, since, until, thread_key, attributed_user_id, model, tool_name, run_id, limit, offset)

    async def list_tool_call_usage(self, since: datetime, until: datetime | None = None, *, thread_key: str | None = None, attributed_user_id: str | None = None, tool_name: str | None = None, run_id: str | None = None, limit: int = 200, offset: int = 0) -> list[ToolCallUsage]:
        return await self._usage_list("tool_call_usage", ToolCallUsage, since, until, thread_key, attributed_user_id, None, tool_name, run_id, limit, offset)


class SQLiteUsage(UsageMixin):
    pass


class PostgresUsage(UsageMixin):
    _usage_pg = True
