"""Explicit terminal continuation contract; approval decisions remain unchanged."""

from ..context import RunContext
from ..gateway.base import ToolResult, ToolSpec
from ..types import ApprovalStatus, Risk


class ApprovalContinuationTools:
    def __init__(self, store):
        self.store = store

    async def _set(self, ctx: RunContext, args: dict) -> ToolResult:
        approval = await self.store.get_approval(args["approval_id"])
        if (approval is None or approval.thread_key != ctx.thread_key
                or approval.run_id != ctx.run_id or approval.status != ApprovalStatus.PENDING):
            return ToolResult("Approval must be pending and belong to this run and thread.",
                              is_error=True)
        saved = await self.store.transition_approval(
            approval.id, ApprovalStatus.PENDING, ApprovalStatus.PENDING,
            result={"handler_receipt_only": args["receipt_only"]})
        return ToolResult({"saved": saved, "receipt_only": args["receipt_only"]})

    def tools(self):
        return [ToolSpec(
            "approval_set_continuation", "approval.continuation", Risk.WRITE_INTERNAL,
            "Mark your pending approval as terminal only if success completes the task "
            "with no dependent work. Success then shows a receipt; failure, denial or "
            "expiry still resumes the agent. Does not approve or execute anything.",
            {"type": "object", "properties": {
                "approval_id": {"type": "string"}, "receipt_only": {"type": "boolean"}},
             "required": ["approval_id", "receipt_only"], "additionalProperties": False},
            self._set)]
