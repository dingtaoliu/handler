"""Turns RunEvents into RunStep rows and running token/cost totals."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import tiktoken

from ..harness.base import RunEvent, RunEventKind
from ..store.base import Store
from ..types import RunStep, StepKind, TokenUsage
from .pricing import PriceTable
from .ledger import RunAttribution, UsageRecord, ToolCallUsage


class RawSink:
    """Appends each event's raw object to <base_dir>/<run_id>.jsonl."""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self._counts: dict[str, int] = {}

    def append(self, run_id: str, raw: dict[str, Any]) -> str:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self.base_dir / f"{run_id}.jsonl"
        if run_id not in self._counts:
            self._counts[run_id] = _count_lines(path)
        self._counts[run_id] += 1
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(raw, ensure_ascii=False, default=str) + "\n")
        return f"{run_id}.jsonl#L{self._counts[run_id]}"


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as f:
        return sum(1 for _ in f)


def _preview(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."


def _size(value: Any) -> int:
    if value is None:
        return 0
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return len(text.encode("utf-8"))


class StepRecorder:
    def __init__(
        self,
        store: Store,
        run_id: str,
        *,
        model: str | None,
        prices: PriceTable,
        raw_sink: RawSink,
        preview_chars: int = 500,
        attribution: RunAttribution | None = None,
        harness: str | None = None,
    ):
        self.attribution = attribution
        self.harness = harness
        self._request_ids: set[str] = set()
        self._tools: dict[str, ToolCallUsage] = {}
        self.store = store
        self.run_id = run_id
        self.model = model
        self.prices = prices
        self.raw_sink = raw_sink
        self.preview_chars = preview_chars
        self._seq = 0
        self._open: dict[str, RunStep] = {}  # tool call id -> running step
        self._usage = TokenUsage()
        self._cost = 0.0
        self._calls = 0
        self._unpriced = False

    def totals(self) -> tuple[TokenUsage, float | None]:
        """Summed usage and cost; cost is None if nothing (or any call) was unpriced."""
        if self._calls == 0 or self._unpriced:
            return self._usage, None
        return self._usage, self._cost

    async def on_event(self, ev: RunEvent) -> None:
        handler = {
            RunEventKind.TOOL_STARTED: self._tool_started,
            RunEventKind.TOOL_FINISHED: self._tool_finished,
            RunEventKind.ASSISTANT_TEXT: self._text,
            RunEventKind.REASONING: self._text,
            RunEventKind.USAGE: self._usage_event,
            RunEventKind.ERROR: self._error,
        }.get(ev.kind)
        if handler:
            await handler(ev)

    def _ref(self, ev: RunEvent) -> str | None:
        return self.raw_sink.append(self.run_id, ev.raw) if ev.raw is not None else None

    def _next(self, ev: RunEvent, kind: StepKind, name: str, **kw: Any) -> RunStep:
        self._seq += 1
        return RunStep(
            run_id=self.run_id,
            seq=self._seq,
            kind=kind,
            name=name,
            source="stream",
            started_at=ev.at,
            raw_ref=self._ref(ev),
            **kw,
        )

    async def _tool_started(self, ev: RunEvent) -> None:
        d = ev.data
        await self._record_tool(ev, finished=False)
        step = self._next(
            ev,
            StepKind.TOOL_CALL,
            str(d.get("tool", "tool")),
            status="running",
            data={
                "id": d.get("id"),
                "kind": d.get("kind"),
                "input": _preview(d.get("input"), self.preview_chars),
            },
        )
        if d.get("id") is not None:
            self._open[str(d["id"])] = step
        await self.store.add_step(step)

    async def _tool_finished(self, ev: RunEvent) -> None:
        d = ev.data
        await self._record_tool(ev, finished=True)
        output = d.get("output")
        summary = {
            "output": _preview(output, self.preview_chars) if output is not None else None,
            "output_size": _size(output),
            "exit_code": d.get("exit_code"),
        }
        step = self._open.pop(str(d.get("id")), None)
        if step is None:  # finished without a start event
            step = self._next(
                ev, StepKind.TOOL_CALL, str(d.get("tool", "tool")),
                data={"id": d.get("id"), "kind": d.get("kind")},
            )
            self._apply_finish(step, ev, summary)
            await self.store.add_step(step)
            return
        self._apply_finish(step, ev, summary)
        ref = self._ref(ev)
        if ref:
            step.data["finish_raw_ref"] = ref
        await self.store.update_step(step)

    @staticmethod
    def _apply_finish(step: RunStep, ev: RunEvent, summary: dict[str, Any]) -> None:
        step.ended_at = ev.at
        status = str(ev.data.get("status", "ok"))
        step.status = "ok" if status in ("ok", "completed", "success") else "error"
        step.data.update(summary)
        step.data["tool_status"] = status

    async def _text(self, ev: RunEvent) -> None:
        kind = (
            StepKind.ASSISTANT_TEXT
            if ev.kind == RunEventKind.ASSISTANT_TEXT
            else StepKind.REASONING
        )
        text = str(ev.data.get("text", ""))
        step = self._next(
            ev, kind, kind.value, ended_at=ev.at,
            data={"text": _preview(text, self.preview_chars), "chars": len(text)},
        )
        await self.store.add_step(step)

    async def _record_tool(self, ev: RunEvent, *, finished: bool):
        if self.attribution is None or not ev.data.get("id"):
            return
        d, call_id = ev.data, str(ev.data["id"])
        row = self._tools.get(call_id)
        if row is None:
            row = await self.store.get_tool_call_usage(self.run_id, call_id)
        if row is None:
            row = ToolCallUsage(
                id=f"{self.run_id}:tool:{call_id}", run_id=self.run_id,
                thread_key=self.attribution.thread_key, tool_call_id=call_id,
                tool_name=str(d.get("tool", "tool")), request_id=d.get("request_id"),
                started_at=ev.at)
        row.request_id = row.request_id or d.get("request_id")
        if not finished:
            row.input_tokens_estimate = _text_metrics(d.get("input"))[0]
        else:
            row.ended_at = ev.at
            row.status = str(d.get("status") or "unknown")
            tokens, size, images = _text_metrics(d.get("output"))
            row.output_tokens_estimate, row.output_bytes, row.output_images = tokens, size, images
            row.single_read_context_cost_estimate_usd = self.prices.cost(
                self.model, TokenUsage(input_tokens=tokens), ev.at, single_request=True)
        self._tools[call_id] = row
        await self.store.upsert_tool_call_usage(row)

    async def _usage_event(self, ev: RunEvent) -> None:
        usage = ev.usage or TokenUsage()
        d = ev.data
        model = d.get("model") or self.model
        request_id = d.get("request_id")
        if request_id and request_id in self._request_ids:
            return
        pricing = self.prices.snapshot(model, usage, ev.at,
                                      single_request=bool(d.get("single_request")),
                                      service_tier=d.get("service_tier"))
        cost = self.prices.cost(model, usage, ev.at,
                                single_request=bool(ev.data.get("single_request")))
        if pricing["service_tier_basis"] == "unsupported_unpriced":
            cost = None
        if self.attribution is not None:
            row = UsageRecord(
                id=f"{self.run_id}:model:{request_id or 'fallback:' + str(self._seq + 1)}",
                run_id=self.run_id, thread_key=self.attribution.thread_key,
                request_id=request_id, tool_call_id=d.get("tool_call_id"),
                tool_name=d.get("tool_name"), model=model,
                requested_model=d.get("requested_model") or self.model,
                service_tier=d.get("service_tier"), harness=self.harness,
                metering_quality=d.get("metering_quality", "harness_reported"),
                status=d.get("status") or "unknown", started_at=_timestamp(d.get("started_at")),
                ended_at=_timestamp(d.get("ended_at")) or ev.at, occurred_at=ev.at,
                usage=usage, cost_usd=cost, pricing=pricing,
                metadata={k: d[k] for k in ("source_request_id", "timestamp_quality") if k in d})
            if not await self.store.add_usage_record(row):
                return
        if request_id:
            self._request_ids.add(request_id)
        self._usage = self._usage + usage
        self._calls += 1
        if cost is None:
            self._unpriced = True
        else:
            self._cost += cost
        step = self._next(
            ev, StepKind.LLM_CALL, model or "llm_call", ended_at=ev.at,
            model=model, usage=usage, cost_usd=cost,
            data={k: d[k] for k in ("request_id", "tool_call_id", "tool_name", "metering_quality") if k in d},
        )
        await self.store.add_step(step)

    async def _error(self, ev: RunEvent) -> None:
        step = self._next(
            ev, StepKind.ERROR, "error", ended_at=ev.at, status="error",
            data={
                "message": _preview(ev.data.get("message", ""), self.preview_chars),
                "retryable": ev.data.get("retryable", False),
            },
        )
        await self.store.add_step(step)


def _timestamp(value):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _text_metrics(value):
    """Local text estimate only; image and binary data are excluded."""
    images = 0
    def clean(item):
        nonlocal images
        if isinstance(item, dict):
            if item.get("type") in ("image", "image_url", "input_image", "output_image"):
                images += 1
                return "[image]"
            return {k: clean(v) for k, v in item.items() if k not in ("base64", "b64_json")}
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item]
        if isinstance(item, str) and item.startswith("data:"):
            return "[data omitted]"
        return item
    if value is None:
        return 0, 0, 0
    content = clean(value)
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    tokens = len(tiktoken.get_encoding("o200k_base").encode(text, disallowed_special=()))
    return tokens, len(text.encode("utf-8")), images
