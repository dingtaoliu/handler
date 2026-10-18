"""The `notify` tool: notifications for users, delivered through Notifier."""

from collections.abc import Callable

from ..bus import RunEventBus
from ..context import RunContext
from ..gateway.base import ToolResult, ToolSpec
from ..notifications import Notifier
from ..store.sqlite_memory import NotificationStore
from ..types import Risk


class NotifyTools:
    def __init__(
        self, store: NotificationStore, bus: RunEventBus, *,
        users_provider: Callable[[], list[str]] | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.notifier = notifier or Notifier(store, bus, users_provider=users_provider)

    async def _notify(self, ctx: RunContext, args: dict) -> ToolResult:
        recipients = args["to"]
        if recipients == ["all"]:
            recipients = "all"
        try:
            _, rows = await self.notifier.send(ctx.thread_key, ctx.run_id, args["message"],
                                               recipients)
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult({"notification_ids": [row.id for row in rows]})

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(
            "notify", "notify.send", Risk.WRITE_INTERNAL, "Notify users about something worth their attention",
            {"type": "object", "properties": {
                "message": {"type": "string"},
                "to": {"type": "array", "items": {"type": "string"},
                       "description": "User ids to notify, or [\"all\"] for every user."},
            }, "required": ["message", "to"], "additionalProperties": False}, self._notify,
        )]
