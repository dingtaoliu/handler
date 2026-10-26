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
                description=("Schedule work for yourself: at each firing you receive `prompt` as a new "
                             "turn in the automation's own thread. Use for reminders, recurring "
                             "checks, digests and follow-ups."),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Short label, e.g. 'daily digest'."},
                        "prompt": {"type": "string", "description": "Full instructions for the "
                                   "future run; it won't see this conversation."},
                        "schedule": {"type": "string", "description": "One of: 'every 30m' "
                                     "(s/m/h/d), 'cron: 0 7 * * 1-5' (5-field cron in "
                                     "`timezone`), or 'at: 2026-10-20T09:00' (runs once)."},
                        "timezone": {"type": "string", "default": "UTC",
                                     "description": "IANA zone, e.g. 'America/New_York'."},
                        "notify": {"type": "array", "items": {"type": "string"},
                                   "description": "User ids to report results to."},
                    },
                    "required": ["name", "prompt", "schedule"],
                    "additionalProperties": False,
                }, handler=self._create,
            ),
            ToolSpec(
                name="automation_list", action="automation.list", risk=Risk.READ,
                description="List automations with their id, schedule, next run and whether enabled.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self._list,
            ),
            ToolSpec(
                name="automation_delete", action="automation.delete", risk=Risk.WRITE_INTERNAL,
                description="Delete an automation by id (see automation_list).",
                input_schema={
                    "type": "object", "properties": {"schedule_id": {"type": "string"}},
                    "required": ["schedule_id"], "additionalProperties": False,
                }, handler=self._delete,
            ),
        ]

    async def _create(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        owner = ctx.acting_as or ctx.user_id
        if owner is None:
            return ToolResult("A user identity is required for an automation", is_error=True)
        now = self.scheduler.clock()
        tz = args.get("timezone", "UTC")
        try:
            next_run, one_shot = parse_spec(args["schedule"], tz, now)
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        schedule = Schedule(
            name=args["name"], prompt=args["prompt"], spec=args["schedule"], timezone=tz,
            notify=args.get("notify", []), created_by=owner,
            next_run_at=next_run, one_shot=one_shot, created_at=now,
        )
        await self.store.create_schedule(schedule)
        self.scheduler.wake()
        return ToolResult({
            "schedule_id": schedule.id,
            "next_run_at": next_run.isoformat() if next_run else None,
        })

    async def _list(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        owner = ctx.acting_as or ctx.user_id
        schedules = await self.store.list_schedules(include_disabled=True)
        return ToolResult([
            {"id": schedule.id, "name": schedule.name, "spec": schedule.spec,
             "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
             "enabled": schedule.enabled}
            for schedule in schedules if schedule.created_by == owner
        ])

    async def _delete(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        owner = ctx.acting_as or ctx.user_id
        schedule = await self.store.get_schedule(args["schedule_id"])
        deleted = bool(schedule and schedule.created_by == owner
                       and await self.store.delete_schedule(args["schedule_id"]))
        self.scheduler.wake()
        return ToolResult({"schedule_id": args["schedule_id"], "deleted": deleted})
