"""Runs that failed for good, kept for the operator (both stores).

A run that fails with no retry left (or with an error that is never retried) is
accepted: its events are marked consumed by the failed run, so they never run
again by themselves, and the thread stays claimable for new work. One
`run_failures` row records it: the error, the attempts it got and the events it
took. `replay_failed_run` (in each store's core) makes those events pending
again, once: the operator's replay and the chat owner's retry share the one
`replayed_at` flag (`replayed_by` says who; a retry records "retry:<user>"). Nothing here notifies anyone; the operator's signal is the
`run failed` ERROR log line (run.py `log_run_failed`).

Threads parked by older releases (`threads.dead`) are swept at startup
(`sweep_dead_threads`): their queued events are consumed by FAILED_BEFORE_UPGRADE,
so stale work does not suddenly run, and each gets a row here to replay.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

# events.consumed_by_run for work queued on a thread an older release parked.
FAILED_BEFORE_UPGRADE = "failed-before-upgrade"
# events.consumed_by_run for a terminal failure recorded without a run id.
FAILED_MARKER = "failed"
# replay_failed_run's `reason` when it did not replay: the failure was replayed
# (or retried) before; or, with latest_only, the run is not the thread's newest
# or the thread has other work queued or running.
ALREADY_REPLAYED = "already_replayed"
NOT_LATEST = "not_latest"


def _date(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def failure_row(row: Any) -> dict:
    row = dict(row)
    ids = row.get("event_ids")
    row["event_ids"] = json.loads(ids) if isinstance(ids, str) else list(ids or [])
    for name in ("failed_at", "replayed_at"):
        row[name] = _date(row.get(name))
    return row


class RunFailures:
    """Reads of `run_failures`. Needs `_thread_query` and `_thread_timestamp`."""

    async def failed_runs(self, *, limit: int = 50,
                          before: tuple[datetime, str] | None = None) -> list[dict]:
        """Terminal failures, newest first; `before` is (failed_at, run_id) of the
        last row of the previous page."""
        where, params = "", []
        if before is not None:
            stamp = self._thread_timestamp(before[0])
            where = "WHERE (failed_at < ? OR (failed_at = ? AND run_id < ?)) "
            params = [stamp, stamp, before[1]]
        rows = await self._thread_query(
            "SELECT run_id, thread_key, thread_type, error, attempts, event_ids, consumed_by, "
            f"failed_at, replayed_at, replayed_by FROM run_failures {where}"
            "ORDER BY failed_at DESC, run_id DESC LIMIT ?", (*params, limit))
        return [failure_row(row) for row in rows]

    async def get_run_failure(self, run_id: str) -> dict | None:
        rows = await self._thread_query(
            "SELECT run_id, thread_key, thread_type, error, attempts, event_ids, consumed_by, "
            "failed_at, replayed_at, replayed_by FROM run_failures WHERE run_id = ?", (run_id,))
        return failure_row(rows[0]) if rows else None
