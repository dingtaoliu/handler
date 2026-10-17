"""Tools for creating, listing and deleting scheduled automations."""

from __future__ import annotations

from typing import Any

from ..context import RunContext
from ..gateway.base import ToolResult, ToolSpec
from ..scheduler import Scheduler, parse_spec
from ..store.base import ScheduleStore
from ..types import Risk, Schedule


class AutomationTools:
    def __init__(self, store: ScheduleStore, scheduler: Scheduler) -> None:
        self.store = store
        self.scheduler = scheduler

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="automation_create", action="automation.create", risk=Risk.WRITE_INTERNAL,
                description="Create a scheduled automation.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "prompt": {"type": "string"},
                        "schedule": {"type": "string"},
                        "timezone": {"type": "string", "default": "UTC"},
                        "notify": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "prompt", "schedule"],
                    "additionalProperties": False,
                }, handler=self._create,
            ),
            ToolSpec(
                name="automation_list", action="automation.list", risk=Risk.READ,
                description="List enabled and disabled automations.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self._list,
            ),
            ToolSpec(
                name="automation_delete", action="automation.delete", risk=Risk.WRITE_INTERNAL,
                description="Delete a scheduled automation.",
                input_schema={
                    "type": "object", "properties": {"schedule_id": {"type": "string"}},
                    "required": ["schedule_id"], "additionalProperties": False,
                }, handler=self._delete,
            ),
        ]

    async def _create(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        now = self.scheduler.clock()
        tz = args.get("timezone", "UTC")
        try:
            next_run, one_shot = parse_spec(args["schedule"], tz, now)
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        schedule = Schedule(
            name=args["name"], prompt=args["prompt"], spec=args["schedule"], timezone=tz,
            notify=args.get("notify", []), created_by=ctx.acting_as or ctx.user_id,
            next_run_at=next_run, one_shot=one_shot, created_at=now,
        )
        await self.store.create_schedule(schedule)
        self.scheduler.wake()
        return ToolResult({
            "schedule_id": schedule.id,
            "next_run_at": next_run.isoformat() if next_run else None,
        })

    async def _list(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        schedules = await self.store.list_schedules(include_disabled=True)
        return ToolResult([
            {"id": schedule.id, "name": schedule.name, "spec": schedule.spec,
             "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
             "enabled": schedule.enabled}
            for schedule in schedules
        ])

    async def _delete(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        deleted = await self.store.delete_schedule(args["schedule_id"])
        self.scheduler.wake()
        return ToolResult({"schedule_id": args["schedule_id"], "deleted": deleted})
