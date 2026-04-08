"""Shared utility functions with no intra-package dependencies."""

from datetime import datetime, timedelta, timezone


def parse_interval(schedule: str) -> timedelta:
    """Parse a simple interval string like '30m', '2h', '1d' into a timedelta."""
    schedule = schedule.strip().lower()
    if schedule.endswith("m"):
        return timedelta(minutes=int(schedule[:-1]))
    elif schedule.endswith("h"):
        return timedelta(hours=int(schedule[:-1]))
    elif schedule.endswith("d"):
        return timedelta(days=int(schedule[:-1]))
    elif schedule.endswith("s"):
        return timedelta(seconds=int(schedule[:-1]))
    raise ValueError(
        f"Invalid schedule format: {schedule!r}. Use e.g. '30m', '2h', '1d'."
    )


def next_run_from_now(schedule: str) -> str:
    """Compute the next run time as a UTC ISO string."""
    delta = parse_interval(schedule)
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%d %H:%M:%S")
