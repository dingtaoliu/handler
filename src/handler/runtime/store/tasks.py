"""Task queries: runs of non-chat threads, with a derived status (both stores).

A *task* is one run of background work (automation, email, crontal, gmail, ...).
Nothing new is stored: tasks are read from runs, threads, events, messages and
approvals. Mixed into the stores next to ThreadMetadata, whose `_thread_query`
and `_thread_timestamp` give one SQL text for SQLite and Postgres.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..types import Event, ThreadSpec, ThreadType

TASK_STATUSES = ("queued", "running", "waiting_approval", "waiting_answer", "succeeded",
                 "failed", "dead")
RUN_STATUSES = TASK_STATUSES[1:]  # every status a run row can derive

# Failed (or dead) first; a live lease means running; a finished run with a
# pending approval, or else a pending question (ask_user), is waiting on a
# human; a "running" row whose lease is gone was abandoned by its worker and
# counts as failed.
_STATUS = """CASE
  WHEN r.status IN ('failed','cancelled','abandoned') AND t.dead AND r.id = (
       SELECT x.id FROM runs x WHERE x.thread_key = r.thread_key
       ORDER BY x.started_at DESC, x.id DESC LIMIT 1) THEN 'dead'
  WHEN r.status IN ('failed','cancelled','abandoned') THEN 'failed'
  WHEN r.status = 'running' AND t.lease_version = r.lease_version
       AND t.lease_until IS NOT NULL AND t.lease_until > ? THEN 'running'
  WHEN EXISTS (SELECT 1 FROM approvals a WHERE a.status = 'pending'
       AND a.run_id = r.id) THEN 'waiting_approval'
  WHEN EXISTS (SELECT 1 FROM questions q
       WHERE q.status = 'pending' AND q.run_id = r.id) THEN 'waiting_answer'
  WHEN r.status = 'running' THEN 'failed'
  ELSE 'succeeded' END"""

_SELECT = f"""SELECT r.id AS run_id, r.thread_key, r.trigger_event_ids, r.status AS run_status,
  r.harness, r.model, r.started_at, r.finished_at, r.error, r.usage, r.cost_usd,
  t.type AS thread_type, {_STATUS} AS task_status,
  (SELECT m.blocks FROM messages m WHERE m.thread_key = r.thread_key AND m.run_id = r.id
     AND m.role = 'assistant' ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS final_blocks,
  (SELECT s.data FROM run_steps s WHERE s.run_id = r.id AND s.kind = 'assistant_text'
     ORDER BY s.seq DESC LIMIT 1) AS last_step_data
FROM runs r JOIN threads t ON t.key = r.thread_key"""


def announce_routed(store: Any, event: Event, thread: ThreadSpec | None) -> None:
    """Call the store's `on_routed` hook after an event was durably routed.

    Used for live task updates ("queued"); best effort and never raises.
    """
    hook = getattr(store, "on_routed", None)
    if hook is None or thread is None:
        return
    try:
        hook(event, thread)
    except Exception:  # noqa: BLE001
        pass


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _date(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


class TaskQueries:
    """Read-only task rows. Needs `_thread_query`, `_thread_timestamp`, `_clock`."""

    async def task_rows(
        self, *, types: list[str] | None = None, statuses: list[str] | None = None,
        since: datetime | None = None, before: tuple[datetime, str] | None = None,
        run_id: str | None = None, limit: int = 50,
    ) -> list[dict]:
        """Runs of non-chat threads, newest first, with their derived task status."""
        clauses, params = [f"t.type <> '{ThreadType.CHAT.value}'"], [self._thread_timestamp(self._clock())]
        if run_id is not None:
            clauses.append("r.id = ?")
            params.append(run_id)
        if types:
            clauses.append(f"t.type IN ({','.join('?' for _ in types)})")
            params += types
        if since is not None:
            clauses.append("r.started_at >= ?")
            params.append(self._thread_timestamp(since))
        if before is not None:
            stamp = self._thread_timestamp(before[0])
            clauses.append("(r.started_at < ? OR (r.started_at = ? AND r.id < ?))")
            params += [stamp, stamp, before[1]]
        query = f"SELECT * FROM ({_SELECT} WHERE {' AND '.join(clauses)}) q"
        if statuses:
            query += f" WHERE task_status IN ({','.join('?' for _ in statuses)})"
            params += statuses
        query += " ORDER BY started_at DESC, run_id DESC LIMIT ?"
        params.append(limit)
        rows = await self._thread_query(query, tuple(params))
        for row in rows:
            for name in ("trigger_event_ids", "usage", "final_blocks", "last_step_data"):
                row[name] = _json(row[name])
            for name in ("started_at", "finished_at"):
                row[name] = _date(row[name])
        return rows

    async def queued_task_rows(
        self, *, types: list[str] | None = None, since: datetime | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Non-chat threads with pending events that no worker holds right now."""
        now = self._thread_timestamp(self._clock())
        clauses = [f"t.type <> '{ThreadType.CHAT.value}'", "t.pending_since IS NOT NULL",
                   "NOT t.dead", "(t.lease_until IS NULL OR t.lease_until <= ?)"]
        params: list[Any] = [now]
        if types:
            clauses.append(f"t.type IN ({','.join('?' for _ in types)})")
            params += types
        if since is not None:
            clauses.append("t.pending_since >= ?")
            params.append(self._thread_timestamp(since))
        rows = await self._thread_query(
            "SELECT t.key AS thread_key, t.type AS thread_type, t.pending_since, "
            "t.available_at, t.attempts, t.last_error FROM threads t "
            f"WHERE {' AND '.join(clauses)} ORDER BY t.pending_since DESC, t.key DESC LIMIT ?",
            (*params, limit))
        for row in rows:
            for name in ("pending_since", "available_at"):
                row[name] = _date(row[name])
        return rows

    async def task_events(self, *, ids: list[str] | None = None,
                          pending_for: list[str] | None = None) -> list[dict]:
        """Trigger events by id, or the unconsumed routed events of some threads."""
        if ids:
            where, params = f"id IN ({','.join('?' for _ in ids)})", list(ids)
        elif pending_for:
            where = (f"thread_key IN ({','.join('?' for _ in pending_for)}) "
                     "AND status = 'routed' AND consumed_by_run IS NULL")
            params = list(pending_for)
        else:
            return []
        rows = await self._thread_query(
            "SELECT id, type, source, payload, user_id, thread_key, received_at FROM events "
            f"WHERE {where} ORDER BY received_at, id", tuple(params))
        for row in rows:
            row["payload"] = _json(row["payload"]) or {}
            row["received_at"] = _date(row["received_at"])
        return rows

    async def task_questions(self, run_ids: list[str], *,
                             pending_only: bool = False) -> list[dict]:
        """Questions asked by these runs (ask_user), oldest first."""
        if not run_ids:
            return []
        status = " AND status = 'pending'" if pending_only else ""
        rows = await self._thread_query(
            "SELECT id, run_id, question, options, allow_text, status, answer, answered_by, "
            "answered_at, expires_at, untrusted, created_at FROM questions "
            f"WHERE run_id IN ({','.join('?' for _ in run_ids)}){status} ORDER BY created_at, id",
            tuple(run_ids))
        for row in rows:
            row["options"], row["answer"] = _json(row["options"]), _json(row["answer"])
            row["allow_text"], row["untrusted"] = bool(row["allow_text"]), bool(row["untrusted"])
            for name in ("answered_at", "expires_at", "created_at"):
                row[name] = _date(row[name])
        return rows

    async def task_approvals(self, run_ids: list[str], *, pending_only: bool = False) -> list[dict]:
        """Approvals requested by these runs, oldest first."""
        if not run_ids:
            return []
        status = " AND status = 'pending'" if pending_only else ""
        rows = await self._thread_query(
            "SELECT id, run_id, tool, action, args, requested_by, reason, status, decided_by, "
            "decided_at, expires_at, created_at FROM approvals "
            f"WHERE run_id IN ({','.join('?' for _ in run_ids)}){status} ORDER BY created_at, id",
            tuple(run_ids))
        for row in rows:
            row["args"] = _json(row["args"])
            for name in ("decided_at", "expires_at", "created_at"):
                row[name] = _date(row[name])
        return rows
