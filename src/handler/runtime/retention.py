"""The 90-day retention cleanup (role-split-design.md, owner decision 9).

A `retention.cleanup` due row per tenant, once a day (host timer). It deletes, for
records older than 90 days:

- `run_failures` rows: the operator's list and replay record of runs that failed
  for good. The run row, its steps and the conversation's messages stay, and so do
  the events the run consumed (they are the thread's input record, and may be email);
- dead `deliveries` (outbox rows that ran out of attempts and were not redelivered);
- events stored with status `over_limit`: inputs dropped over a free plan's limit,
  kept only as a record so a resend stays a duplicate.

It never deletes stored email that hasn't been processed: over-limit records of
email types are kept (the never-drop-email rule; free plans have no email channel
anyway), held inputs wait for release and are not touched, and neither are the email
spool (`handler_control.spooled_email`) nor connected mailboxes' mail.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .types import EventStatus, utcnow

logger = logging.getLogger("handler.runtime.retention")

RETENTION_DAYS = 90
INTERVAL = timedelta(days=1)
# Inputs that are email: an over-limit record of one is never deleted.
EMAIL_EVENT_TYPES = ("email.received", "mail.received")


def _ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


async def retention_cleanup(store: Any, now: datetime | None = None) -> dict[str, int]:
    """Delete what the retention decision covers; returns counts per table."""
    now = now or utcnow()
    before = now - timedelta(days=RETENTION_DAYS)
    over = EventStatus.OVER_LIMIT.value
    if hasattr(store, "_call"):  # SQLite
        marks = ", ".join("?" for _ in EMAIL_EVENT_TYPES)

        def run(db) -> dict[str, int]:
            db.execute("BEGIN IMMEDIATE")
            try:
                counts = {
                    "run_failures": db.execute(
                        "DELETE FROM run_failures WHERE failed_at < ?", (_ts(before),)).rowcount,
                    "dead_deliveries": db.execute(
                        "DELETE FROM deliveries WHERE status = 'dead' AND updated_at < ?",
                        (_ts(before),)).rowcount,
                    "over_limit_events": db.execute(
                        f"DELETE FROM events WHERE status = ? AND received_at < ? "
                        f"AND type NOT IN ({marks})",
                        (over, _ts(before), *EMAIL_EVENT_TYPES)).rowcount,
                }
                db.commit()
            except BaseException:
                db.rollback()
                raise
            return counts

        counts = await store._call(run)
    else:
        async with store._conn() as conn:
            counts = {}
            for name, query, params in (
                    ("run_failures", "DELETE FROM run_failures WHERE failed_at < %s", (before,)),
                    ("dead_deliveries", "DELETE FROM deliveries WHERE status = 'dead' "
                     "AND updated_at < %s", (before,)),
                    ("over_limit_events", "DELETE FROM events WHERE status = %s "
                     "AND received_at < %s AND NOT (type = ANY(%s)) "
                     "AND id NOT IN (SELECT event_id FROM held_inputs)",
                     (over, before, list(EMAIL_EVENT_TYPES)))):
                cursor = await conn.execute(query, params)
                counts[name] = cursor.rowcount
    if any(counts.values()):
        logger.info("retention cleanup", extra={"retention_days": RETENTION_DAYS, **counts})
    return counts


async def retention_due(store: Any) -> datetime:
    """The `retention.cleanup` handler: clean up, then come back in a day."""
    await retention_cleanup(store)
    return utcnow() + INTERVAL
