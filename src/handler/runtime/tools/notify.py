"""Persist notifications and publish live updates for recipients."""

from collections.abc import Callable
from dataclasses import asdict

from ..bus import BusMessage, RunEventBus
from ..context import RunContext
from ..gateway.base import ToolResult, ToolSpec
from ..store.sqlite_memory import NotificationStore
from ..types import Risk


class NotifyTools:
    def __init__(
        self, store: NotificationStore, bus: RunEventBus, *,
        users_provider: Callable[[], list[str]] | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.users_provider = users_provider

    async def _notify(self, ctx: RunContext, args: dict) -> ToolResult:
        recipients = args["to"]
        if recipients == "all":
            if self.users_provider is None:
                return ToolResult("Broadcast needs a users_provider", is_error=True)
            recipients = self.users_provider()
        if not isinstance(recipients, list) or not all(isinstance(user, str) for user in recipients):
            return ToolResult("to must be a list of user ids or 'all'", is_error=True)
        ids = []
        for user in dict.fromkeys(recipients):
            notification = await self.store.add_notification(
                user, ctx.thread_key, ctx.run_id, args["message"],
            )
            data = asdict(notification)
            data["created_at"] = notification.created_at.isoformat()
            self.bus.publish(BusMessage(ctx.thread_key, ctx.run_id, "notification", data))
            ids.append(notification.id)
        return ToolResult({"notification_ids": ids})

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(
            "notify", "notify.send", Risk.WRITE_INTERNAL, "Notify users about something worth their attention",
            {"type": "object", "properties": {
                "message": {"type": "string"},
                "to": {"oneOf": [{"const": "all"}, {"type": "array", "items": {"type": "string"}}]},
            }, "required": ["message", "to"], "additionalProperties": False}, self._notify,
        )]
