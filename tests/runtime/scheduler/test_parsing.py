from datetime import datetime, timedelta, timezone

import pytest

from handler.runtime.scheduler import parse_spec


@pytest.mark.parametrize("unit,seconds", [("s", 1), ("m", 60), ("h", 3600), ("d", 86400)])
def test_intervals(clock, unit, seconds):
    assert parse_spec(f"every 3{unit}", "UTC", clock()) == (
        clock() + timedelta(seconds=3 * seconds), False,
    )


def test_intervals_use_elapsed_time_in_utc():
    after = datetime.fromisoformat("2026-03-07T12:00:00-05:00")
    assert parse_spec("every 1d", "America/New_York", after) == (
        datetime(2026, 3, 8, 17, tzinfo=timezone.utc), False,
    )


@pytest.mark.parametrize("tz,hour", [("UTC", 7), ("America/New_York", 11), ("Asia/Tokyo", 22)])
def test_cron_timezones(tz, hour):
    after = datetime(2026, 10, 16, 0, tzinfo=timezone.utc)
    expected = datetime(2026, 10, 16 if hour != 22 else 18, hour, tzinfo=timezone.utc)
    # Friday 07:00 in Tokyo has already passed; the next weekday is Monday.
    assert parse_spec("cron: 0 7 * * 1-5", tz, after) == (expected, False)


def test_cron_strictly_after_and_winter_offset():
    after = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
    assert parse_spec("cron: 0 7 * * *", "America/New_York", after) == (
        datetime(2026, 12, 2, 12, tzinfo=timezone.utc), False,
    )


@pytest.mark.parametrize("value,tz,expected", [
    ("2026-10-17T09:30:00Z", "America/New_York", "2026-10-17T09:30:00+00:00"),
    ("2026-10-17T09:30:00+02:00", "UTC", "2026-10-17T07:30:00+00:00"),
    ("2026-10-17T09:30:00", "America/New_York", "2026-10-17T13:30:00+00:00"),
    ("2026-12-17T09:30:00", "America/New_York", "2026-12-17T14:30:00+00:00"),
])
def test_at(clock, value, tz, expected):
    assert parse_spec(f"at: {value}", tz, clock()) == (datetime.fromisoformat(expected), True)


@pytest.mark.parametrize("spec,match", [
    ("tomorrow", "Use"), ("every 0s", "greater than zero"),
    ("every -2h", "Use"), ("every 1.5h", "Use"), ("every 2w", "Use"),
    ("every 999999999999999999999d", "too large"),
    ("cron: * * * * * *", "5-field"), ("cron: @daily", "5-field"),
    ("cron: 70 1 * * *", "Invalid cron"), ("cron: 0 0 31 2 *", "Invalid cron"),
    ("at: nonsense", "ISO-8601"), ("at:", "ISO-8601"),
])
def test_invalid_specs(clock, spec, match):
    with pytest.raises(ValueError, match=match):
        parse_spec(spec, "UTC", clock())


def test_invalid_timezone_and_naive_after(clock):
    with pytest.raises(ValueError, match="Unknown timezone"):
        parse_spec("every 30m", "Nowhere/Nope", clock())
    with pytest.raises(ValueError, match="after must include"):
        parse_spec("every 30m", "UTC", clock().replace(tzinfo=None))
