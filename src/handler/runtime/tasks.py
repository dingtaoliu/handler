"""Tasks: the user-facing view of background (non-chat) runs.

A task is one run of a non-chat thread (automation, email, crontal, gmail, ...),
or a non-chat thread with pending events that no run has picked up yet
("queued"). Everything is derived from the store; see store/tasks.py and
docs/rebuild/tasks.md.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import datetime
from typing import Any

from .bus import BusMessage, RunEventBus
from .store.tasks import RUN_STATUSES, TASK_STATUSES
from .types import Event, ThreadSpec, ThreadType, utcnow

logger = logging.getLogger("handler.runtime.tasks")

TASK_UPDATED = "task_updated"
SUMMARY_CHARS = 300
TRACE_CHARS = 2000
MAX_LIMIT = 200
QUEUED_LIMIT = 50
TASK_TYPES = tuple(t.value for t in ThreadType if t is not ThreadType.CHAT)

__all__ = ["TASK_STATUSES", "TASK_TYPES", "TASK_UPDATED", "list_tasks", "get_task",
           "trigger_summary", "publish_task", "publish_queued", "encode_cursor",
           "decode_cursor"]


# ─── Trigger summaries ──────────────────────────────────────────────────────
def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _who(address: Any) -> str:
    if not isinstance(address, dict):
        return ""
    name, addr = (address.get("name") or "").strip(), (address.get("address") or "").strip()
    if name and addr and name != addr:
        return f"{name} <{addr}>"
    return addr or name


def _email_summary(payload: dict) -> str:
    original = payload.get("original")
    if not isinstance(original, dict):
        originals = payload.get("originals") or []
        original = originals[0] if originals and isinstance(originals[0], dict) else {}
    sender = _who(original.get("from")) or _who(payload.get("forwarder")) or "unknown sender"
    subject = original.get("subject") or payload.get("subject") or "(no subject)"
    return f"Email from {sender}: {subject}"


def _event_summary(event: dict) -> str:
    kind, payload = event.get("type") or "", event.get("payload") or {}
    if kind == "email.received":
        return _email_summary(payload)
    if kind == "automation.fired":
        name = payload.get("name") or payload.get("schedule_id") or "automation"
        return f"{name} (run manually)" if payload.get("manual") else str(name)
    if kind == "crontal.notification":
        entity = " ".join(str(v) for v in (payload.get("entity_type"), payload.get("entity_id"))
                          if v not in (None, ""))
        note = payload.get("type") or "notification"
        return f"{entity or 'Crontal'} · {note}"
    if kind == "approval.resolved":
        return f"Approval {payload.get('status') or 'resolved'}: {payload.get('tool') or 'tool'}"
    if kind == "chat.message":
        return _clip(payload.get("text"), 120) or "Message"
    for field in ("name", "subject", "title", "text"):
        if payload.get(field):
            return f"{kind}: {_clip(payload[field], 120)}"
    return f"{kind or 'event'} from {event.get('source') or 'unknown'}"


def trigger_summary(events: list[dict]) -> str:
    """Human-readable cause of a run, from its trigger events (oldest first)."""
    if not events:
        return "No trigger recorded"
    summary = _event_summary(events[0])
    extra = len(events) - 1
    return _clip(summary, 240) + (f" (+{extra} more)" if extra > 0 else "")


# ─── Items ──────────────────────────────────────────────────────────────────
def _final_text(row: dict) -> str | None:
    blocks = row.get("final_blocks") or []
    text = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    if not text and isinstance(row.get("last_step_data"), dict):
        text = row["last_step_data"].get("text") or ""
    return _clip(text, SUMMARY_CHARS) or None


def _ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _tokens(usage: Any) -> dict | None:
    if not isinstance(usage, dict):
        return None
    out = {k: int(usage.get(k) or 0) for k in (
        "input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens",
        "reasoning_tokens")}
    out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    return out


def _run_item(row: dict, events: list[dict], titles: dict[str, dict], pending: list[str],
              now: datetime) -> dict:
    status = row["task_status"]
    error = row.get("error")
    if status in ("failed", "dead") and row["run_status"] in ("running", "abandoned"):
        error = error or "Run was interrupted: its worker stopped before finishing"
    finished = row.get("finished_at")
    return {
        "id": row["run_id"],
        "run_id": row["run_id"],
        "thread_key": row["thread_key"],
        "thread_type": row["thread_type"],
        "thread_title": (titles.get(row["thread_key"]) or {}).get("title") or row["thread_key"],
        "trigger_summary": trigger_summary(events),
        "trigger_events": [{"id": e["id"], "type": e["type"], "source": e["source"],
                            "received_at": e["received_at"]} for e in events],
        "status": status,
        "run_status": str(row["run_status"]),
        "harness": row.get("harness"),
        "model": row.get("model"),
        "queued_at": min((e["received_at"] for e in events if e.get("received_at")), default=None),
        "started_at": row["started_at"],
        "finished_at": finished,
        "duration_ms": _ms(row["started_at"], finished or (now if status == "running" else None)),
        "cost_usd": row.get("cost_usd"),
        "usage": _tokens(row.get("usage")),
        "summary": _final_text(row),
        "error": error,
        "pending_approval_ids": pending,
    }


def _queued_item(row: dict, events: list[dict], titles: dict[str, dict]) -> dict:
    key = row["thread_key"]
    return {
        "id": f"queued:{key}",
        "run_id": None,
        "thread_key": key,
        "thread_type": row["thread_type"],
        "thread_title": (titles.get(key) or {}).get("title") or key,
        "trigger_summary": trigger_summary(events),
        "trigger_events": [{"id": e["id"], "type": e["type"], "source": e["source"],
                            "received_at": e["received_at"]} for e in events],
        "status": "queued",
        "run_status": None,
        "harness": None,
        "model": None,
        "queued_at": row["pending_since"],
        "started_at": None,
        "finished_at": None,
        "duration_ms": None,
        "cost_usd": None,
        "usage": None,
        "summary": None,
        # A retry after a failed run waits for available_at.
        "error": row.get("last_error"),
        "retry_at": row.get("available_at") if row.get("attempts") else None,
        "attempts": row.get("attempts") or 0,
        "pending_approval_ids": [],
    }


def encode_cursor(started_at: datetime, run_id: str) -> str:
    raw = json.dumps([started_at.isoformat(), run_id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        started, run_id = json.loads(raw)
        stamp = datetime.fromisoformat(started)
        if stamp.tzinfo is None or not isinstance(run_id, str):
            raise ValueError
        return stamp, run_id
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


async def _items(store: Any, rows: list[dict], queued: list[dict], user_id: str | None) -> list[dict]:
    ids = sorted({i for row in rows for i in (row.get("trigger_event_ids") or [])})
    events = {e["id"]: e for e in await store.task_events(ids=ids)} if ids else {}
    pending_events: dict[str, list[dict]] = {}
    if queued:
        for e in await store.task_events(pending_for=[q["thread_key"] for q in queued]):
            pending_events.setdefault(e["thread_key"], []).append(e)
    waiting = [r["run_id"] for r in rows if r["task_status"] == "waiting_approval"]
    approvals: dict[str, list[str]] = {}
    for a in await store.task_approvals(waiting, pending_only=True) if waiting else []:
        approvals.setdefault(a["run_id"], []).append(a["id"])
    keys = sorted({r["thread_key"] for r in rows} | {q["thread_key"] for q in queued})
    titles = await store.thread_metadata(keys, user_id) if keys else {}
    now = utcnow()
    items = [_queued_item(q, pending_events.get(q["thread_key"], []), titles) for q in queued]
    for row in rows:
        trigger = sorted((events[i] for i in row.get("trigger_event_ids") or [] if i in events),
                         key=lambda e: (e["received_at"], e["id"]))
        items.append(_run_item(row, trigger, titles, approvals.get(row["run_id"], []), now))
    return items


def _validated(values: list[str] | None, allowed: tuple[str, ...], name: str) -> list[str] | None:
    if not values:
        return None
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise ValueError(f"unknown {name}: {', '.join(unknown)} (use {', '.join(allowed)})")
    return sorted(set(values))


async def list_tasks(
    store: Any, *, user_id: str | None, types: list[str] | None = None,
    statuses: list[str] | None = None, since: datetime | None = None,
    cursor: str | None = None, limit: int = 50,
) -> dict:
    """One page of tasks, newest first. Raises ValueError on bad filters.

    Queued tasks (no run yet) lead the first page only; `limit` and the cursor
    apply to runs.
    """
    types = _validated(types, TASK_TYPES, "type")
    statuses = _validated(statuses, TASK_STATUSES, "status")
    if since is not None and since.tzinfo is None:
        raise ValueError("since must include a timezone")
    limit = max(1, min(int(limit), MAX_LIMIT))
    before = decode_cursor(cursor) if cursor else None
    run_statuses = None if statuses is None else [s for s in statuses if s in RUN_STATUSES]
    rows = [] if run_statuses == [] else await store.task_rows(
        types=types, statuses=run_statuses, since=since, before=before,
        owner_user_id=user_id, limit=limit)
    include_queued = before is None and (statuses is None or "queued" in statuses)
    queued = await store.queued_task_rows(types=types, since=since,
                                          owner_user_id=user_id, limit=QUEUED_LIMIT) \
        if include_queued else []
    items = await _items(store, rows, queued, user_id)
    last = rows[-1] if len(rows) == limit else None
    return {
        "items": items,
        "next_cursor": encode_cursor(last["started_at"], last["run_id"]) if last else None,
    }


# ─── Detail and debug trace ─────────────────────────────────────────────────
def _truncate(value: Any, limit: int = TRACE_CHARS, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"… [{len(value)} chars]"
    if depth > 6:
        return _truncate(json.dumps(value, default=str), limit)
    if isinstance(value, dict):
        return {k: _truncate(v, limit, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, limit, depth + 1) for v in value[:100]]
    return value


def _step(step: Any, previous_end: datetime | None) -> dict:
    kind = str(step.kind)
    item = {
        "seq": step.seq, "kind": kind, "name": step.name, "status": step.status,
        "source": step.source, "started_at": step.started_at, "ended_at": step.ended_at,
        "duration_ms": _ms(step.started_at, step.ended_at),
        "model": step.model,
        "usage": _tokens(step.usage.__dict__) if step.usage is not None else None,
        "cost_usd": step.cost_usd,
        "parent_seq": step.parent_seq,
        "raw_ref": step.raw_ref,
        "data": _truncate(step.data or {}),
    }
    if kind == "llm_call":
        # Usage arrives when the model call finishes: the gap since the previous
        # step approximates the call's latency.
        item["latency_ms"] = _ms(previous_end, step.started_at)
    return item


def _approval_step(approval: dict) -> dict:
    return {
        "seq": None, "kind": "approval", "name": approval["tool"], "status": approval["status"],
        "source": "gateway", "started_at": approval["created_at"],
        "ended_at": approval.get("decided_at"),
        "duration_ms": _ms(approval["created_at"], approval.get("decided_at")),
        "model": None, "usage": None, "cost_usd": None, "parent_seq": None, "raw_ref": None,
        "data": _truncate({"approval_id": approval["id"], "action": approval["action"],
                           "args": approval.get("args"), "reason": approval.get("reason"),
                           "requested_by": approval.get("requested_by"),
                           "decided_by": approval.get("decided_by"),
                           "expires_at": approval.get("expires_at")}),
    }


async def get_task(store: Any, run_id: str, *, user_id: str | None,
                   include_steps: bool) -> dict | None:
    """One task by run id (non-chat threads only), with `steps` when asked."""
    rows = await store.task_rows(run_id=run_id, owner_user_id=user_id, limit=1)
    if not rows:
        return None
    item = (await _items(store, rows, [], user_id))[0]
    if include_steps:
        previous = rows[0]["started_at"]
        steps = []
        for step in await store.list_steps(run_id):
            steps.append(_step(step, previous))
            previous = step.ended_at or step.started_at
        steps += [_approval_step(a) for a in await store.task_approvals([run_id])]
        steps.sort(key=lambda s: (s["started_at"], s["seq"] if s["seq"] is not None else 1 << 30))
        item["steps"] = steps
    return item


# ─── Live updates ───────────────────────────────────────────────────────────
async def publish_task(bus: RunEventBus, store: Any, thread_key: str, run_id: str) -> None:
    """Publish a run's current task item (without steps) on the bus. Best effort."""
    if not hasattr(store, "task_rows"):
        return
    try:
        item = await get_task(store, run_id, user_id=None, include_steps=False)
    except Exception:  # noqa: BLE001 - live updates must never break a run
        logger.debug("task update failed for %s", run_id, exc_info=True)
        return
    if item is not None:
        bus.publish(BusMessage(thread_key, run_id, TASK_UPDATED, item))


def publish_queued(bus: RunEventBus, event: Event, thread: ThreadSpec) -> None:
    """Store hook: a non-chat thread got new work (a queued task)."""
    if thread.type is ThreadType.CHAT:
        return
    bus.publish(BusMessage(thread.key, None, TASK_UPDATED, {
        "id": f"queued:{thread.key}", "run_id": None, "thread_key": thread.key,
        "thread_type": thread.type.value, "status": "queued", "event_type": event.type,
        "queued_at": event.received_at.isoformat(),
    }))

