"""Conversation naming inside the active run, without an additional model call."""
from ..bus import BusMessage
from ..gateway.base import ToolResult, ToolSpec
from ..types import Risk


class ThreadTools:
    def __init__(self, store, bus):
        self.store, self.bus = store, bus

    async def _set_title(self, ctx, args):
        if not ctx.thread_key.startswith("chat:"):
            return ToolResult("Only chat conversations need generated names", is_error=True)
        try:
            changed = await self.store.set_thread_title(ctx.thread_key, args["title"], manual=False)
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        if changed:
            self.bus.publish(BusMessage(ctx.thread_key, ctx.run_id, "thread_updated", {}))
        return ToolResult({"saved": changed})

    def tools(self):
        return [ToolSpec("thread_set_title", "thread.set_title", Risk.WRITE_INTERNAL,
            "Give this chat a short descriptive name. Existing names are preserved.",
            {"type": "object", "properties": {"title": {"type": "string", "minLength": 1,
             "maxLength": 120}}, "required": ["title"], "additionalProperties": False},
            self._set_title)]
