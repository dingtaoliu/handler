"""Report task progress with references to deterministic gateway receipts."""
from __future__ import annotations

from typing import Any

from ..context import RunContext
from ..gateway.base import ToolResult, ToolSpec
from ..task_outcomes import OUTCOMES, outcome_view
from ..types import Risk


class TaskOutcomeTools:
    def __init__(self, store: Any):
        self.store = store

    async def _read(self, ctx: RunContext, args: dict) -> ToolResult:
        view = await outcome_view(self.store, ctx.run_id)
        if view:
            view["receipt_count"] = len(view["receipts"])
            # Most recent 50 plus unresolved errors; no large tool bodies.
            needed = set(view["unresolved_error_receipt_ids"])
            view["receipts"] = [r for r in view["receipts"][:-50] if r["id"] in needed] + view["receipts"][-50:]
        return ToolResult(view or {"outcome": "unknown", "reason": "No tracked task for this run"})

    async def _report(self, ctx: RunContext, args: dict) -> ToolResult:
        view = await outcome_view(self.store, ctx.run_id)
        if view is None:
            return ToolResult("No tracked task for this run", is_error=True)
        checks = args.get("checks", [])
        receipts = {r["id"]: r for r in view["receipts"]}
        for check in checks:
            receipt = receipts.get(check["receipt_id"])
            if (receipt is None or receipt["status"] != "ok" or
                    check["field"] not in receipt["facts"] or
                    isinstance(receipt["facts"][check["field"]], bool) != isinstance(check["expected"], bool) or
                    receipt["facts"][check["field"]] != check["expected"]):
                return ToolResult("Check does not match a successful receipt fact in this task", is_error=True)
        if args["outcome"] == "completed":
            if (not checks or args["pending_work"] or view["unresolved_error_receipt_ids"] or
                    view["pending_ids"] or view["pending_receipt_ids"] or
                    any(j["state"] != "confirmed" for j in view["jobs"])):
                return ToolResult("Completion needs matching receipt checks, no pending work, errors, "
                                  "approvals/answers or unfinished extraction. Report partial, blocked "
                                  "or awaiting_review instead.", is_error=True)
        await self.store.save_task_report(view["id"], {**args, "receipt_count": len(view["receipts"])})
        result = await outcome_view(self.store, ctx.run_id)
        assert result is not None
        result.pop("receipts", None)
        return ToolResult(result)

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec("task_get_outcome", "task.read", Risk.READ,
            "Read this task's outcome, linked runs/jobs, unresolved errors and compact actual tool "
            "receipts. Receipts contain machine facts, not email bodies or model assertions.",
            {"type": "object", "properties": {}, "additionalProperties": False}, self._read),
            ToolSpec("task_report_outcome", "task.report", Risk.WRITE_INTERNAL,
            "Before finishing material work, report its business outcome separately from execution. "
            "Use task_get_outcome to find receipt facts. Completed requires checks against successful "
            "receipt fields, all requested work verified, and no pending work/errors/approvals/jobs. "
            "For tables compare actual row counts to all source rows; creation of an ID is not "
            "source coverage. SMTP sent means server accepted, not recipient delivery; file creation "
            "does not prove browser download. Use awaiting_review when correctness/coverage is unknown. "
            "This records reporting only and never executes or approves business actions.",
            {"type": "object", "properties": {
                "outcome": {"type": "string", "enum": OUTCOMES},
                "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                "pending_work": {"type": "array", "maxItems": 20,
                                 "items": {"type": "string", "maxLength": 300}},
                "checks": {"type": "array", "maxItems": 30, "items": {
                    "type": "object", "properties": {"receipt_id": {"type": "string"},
                        "field": {"type": "string"},
                        "expected": {"type": ["string", "number", "boolean"]}},
                    "required": ["receipt_id", "field", "expected"], "additionalProperties": False}}},
             "required": ["outcome", "reason", "pending_work"], "additionalProperties": False}, self._report)]
