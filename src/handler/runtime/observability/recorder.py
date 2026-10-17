"""Turns RunEvents into RunStep rows and running token/cost totals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..harness.base import RunEvent, RunEventKind
from ..store.base import Store
from ..types import RunStep, StepKind, TokenUsage
from .pricing import PriceTable


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
    ):
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

    async def _usage_event(self, ev: RunEvent) -> None:
        usage = ev.usage or TokenUsage()
        model = ev.data.get("model") or self.model
        cost = self.prices.cost(model, usage, ev.at)
        self._usage = self._usage + usage
        self._calls += 1
        if cost is None:
            self._unpriced = True
        else:
            self._cost += cost
        step = self._next(
            ev, StepKind.LLM_CALL, model or "llm_call", ended_at=ev.at,
            model=model, usage=usage, cost_usd=cost,
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
