"""Schedule parsing and an event-driven automation scheduler."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from .store.base import ScheduleStore
from .settings import HarnessSource, resolve_harness
from .tenancy.limits import OverLimit
from .types import Event, Lane, Schedule, ThreadSpec, ThreadType, utcnow


logger = logging.getLogger("handler.runtime.scheduler")


def parse_spec(spec: str, tz: str, after: datetime) -> tuple[datetime | None, bool]:
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {tz!r}") from exc
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError("after must include a timezone")
    after = after.astimezone(timezone.utc)
    spec = spec.strip()
    interval = re.fullmatch(r"every ([0-9]+)([smhd])", spec)
    if interval:
        count = int(interval[1])
        if count <= 0:
            raise ValueError("The interval must be greater than zero")
        seconds = count * {"s": 1, "m": 60, "h": 3600, "d": 86400}[interval[2]]
        try:
            return after + timedelta(seconds=seconds), False
        except OverflowError as exc:
            raise ValueError("The interval is too large") from exc
    if spec.startswith("cron:"):
        expression = spec[5:].strip()
        if len(expression.split()) != 5:
            raise ValueError("cron: requires a 5-field expression (minute hour day month weekday)")
        try:
            next_run = croniter(expression, after.astimezone(zone)).get_next(datetime)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid cron expression: {expression!r}: {exc}") from exc
        return next_run.astimezone(timezone.utc), False
    if spec.startswith("at:"):
        try:
            instant = datetime.fromisoformat(spec[3:].strip())
        except ValueError as exc:
            raise ValueError("at: requires an ISO-8601 date/time") from exc
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=zone)
        return instant.astimezone(timezone.utc), True
    raise ValueError("Use 'every <n><s|m|h|d>', 'cron: <5-field expression>', or 'at: <ISO-8601>'")


class Scheduler:
    """Fires automations. The host timer calls `fire_due` for each `schedule.fire` row
    (store/due_work.py), which the schedule's own writes keep at its next_run_at;
    `tick` fires everything due at once (tests, and a catch-up without the timer)."""

    def __init__(
        self,
        store: ScheduleStore,
        *,
        clock: Callable[[], datetime] = utcnow,
        default_harness: HarnessSource = "codex",
    ) -> None:
        self.store = store
        self.clock = clock
        self.default_harness = default_harness
        self.on_skipped: Callable[[Schedule, OverLimit], Awaitable[None]] | None = None
        self.paused: Callable[[], bool] | None = None  # a plan without automations
        self.on_wake: Callable[[], None] | None = None  # the host timer's wake

    def wake(self) -> None:
        """A schedule changed. Its due row was written with it; this only hints."""
        if self.on_wake is not None:
            self.on_wake()

    def _is_paused(self) -> bool:
        return self.paused is not None and self.paused()

    async def tick(self) -> int:
        if self._is_paused():
            return 0  # schedules are kept, and resume when the plan allows them
        now = self.clock().astimezone(timezone.utc)
        fired = 0
        while schedules := await self.store.due_schedules(now):
            for schedule in schedules:
                fired += await self._fire(schedule, now)
        return fired

    async def fire_due(self, schedule_id: str) -> datetime | None:
        """One `schedule.fire` row: fire it if due; return when it is due next.

        A next_run_at long past fires once, then moves to the next slot from now
        (missed intervals are not replayed). A paused schedule's row is dropped:
        the plan change that allows automations again backfills it.
        """
        schedule = await self.store.get_schedule(schedule_id)
        if schedule is None or not schedule.enabled or schedule.next_run_at is None:
            return None
        if self._is_paused():
            return None
        now = self.clock().astimezone(timezone.utc)
        if schedule.next_run_at > now:
            return schedule.next_run_at
        await self._fire(schedule, now)
        current = await self.store.get_schedule(schedule_id)
        return (current.next_run_at if current is not None and current.enabled
                else None)

    async def _fire(self, schedule: Schedule, now: datetime) -> int:
        expected = schedule.next_run_at
        assert expected is not None  # due_schedules excludes null next_run_at
        try:
            next_run, one_shot = parse_spec(schedule.spec, schedule.timezone, expected)
        except ValueError:
            # A bad stored spec must not stop every other automation.
            logger.exception("disabling schedule %s with invalid spec", schedule.id)
            schedule.enabled = False
            await self.store.update_schedule(schedule)
            return 0
        if schedule.one_shot or one_shot:
            next_run = None
        elif next_run is not None and next_run <= now:
            next_run, _ = parse_spec(schedule.spec, schedule.timezone, now)
        key = f"automation:{schedule.id}"
        event = Event(
            type="automation.fired", source="scheduler",
            payload={
                "schedule_id": schedule.id, "name": schedule.name,
                "prompt": schedule.prompt, "fired_at": now.isoformat(),
                "notify": schedule.notify,
                "parent_thread_key": schedule.parent_thread_key,
            },
            idempotency_key=f"automation:{schedule.id}:{expected.isoformat()}",
            thread_key=key, user_id=schedule.created_by, received_at=now,
        )
        thread = ThreadSpec(
            key, ThreadType.AUTOMATION, Lane.BACKGROUND,
            owner_user_id=schedule.created_by,
            harness=schedule.harness or await resolve_harness(self.default_harness),
            model=schedule.model,
        )
        outcome = await self.store.fire_schedule(schedule, expected, next_run, event, thread)
        if isinstance(outcome, OverLimit):
            logger.info("automation skipped: input limit",
                        extra={"schedule": schedule.id, "used": outcome.used})
            if outcome.notify and self.on_skipped is not None:
                try:
                    await self.on_skipped(schedule, outcome)
                except Exception:
                    logger.exception("skip notice failed", extra={"schedule": schedule.id})
            return 0
        return int(outcome)
