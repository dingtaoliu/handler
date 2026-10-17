"""Schedule parsing and an event-driven automation scheduler."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from .store.base import ScheduleStore
from .types import Event, Lane, ThreadSpec, ThreadType, utcnow


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
    def __init__(
        self,
        store: ScheduleStore,
        *,
        clock: Callable[[], datetime] = utcnow,
        default_harness: str = "codex",
        max_sleep_seconds: float = 300,
    ) -> None:
        if max_sleep_seconds <= 0:
            raise ValueError("max_sleep_seconds must be greater than zero")
        self.store = store
        self.clock = clock
        self.default_harness = default_harness
        self.max_sleep_seconds = max_sleep_seconds
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    async def tick(self) -> int:
        now = self.clock().astimezone(timezone.utc)
        fired = 0
        while schedules := await self.store.due_schedules(now):
            for schedule in schedules:
                expected = schedule.next_run_at
                assert expected is not None  # due_schedules excludes null next_run_at
                try:
                    next_run, one_shot = parse_spec(schedule.spec, schedule.timezone, expected)
                except ValueError:
                    # A bad stored spec must not stop every other automation.
                    logger.exception("disabling schedule %s with invalid spec", schedule.id)
                    schedule.enabled = False
                    await self.store.update_schedule(schedule)
                    continue
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
                    harness=schedule.harness or self.default_harness, model=schedule.model,
                )
                fired += await self.store.fire_schedule(schedule, expected, next_run, event, thread)
        return fired

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            # Clear before reading storage, so a wake during a read is retained.
            self._wake.clear()
            try:
                await self.tick()
            except Exception:
                logger.exception("scheduler tick failed")
            next_due = await self.store.next_due_at()
            delay = self.max_sleep_seconds
            if next_due is not None:
                delay = min(delay, max(0.0, (next_due - self.clock()).total_seconds()))
            waiters = [asyncio.create_task(self._wake.wait()), asyncio.create_task(stop.wait())]
            try:
                await asyncio.wait(waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
