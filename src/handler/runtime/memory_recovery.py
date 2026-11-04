"""Recover memory.save calls that waited for an approval and never ran.

Before memory.save ran unasked on untrusted threads, every save on an email,
Crontal or mail thread waited for a human; many expired or are still pending.
An operator can list them and run them now (oldest first, so appends keep
their order), via the admin endpoints in web/memory_recovery_api.py:

    GET  /api/admin/memory-saves?status=pending,expired
    POST /api/admin/memory-saves/recover  {"dry_run": true}

A save whose content is already in the topic (e.g. the agent saved it again
later) is skipped. Replayed saves run in their original thread's context, so
a save from an untrusted thread marks its topic source_untrusted as usual.
"""

from __future__ import annotations

from typing import Any

from .types import Approval, ApprovalStatus

ACTION = "memory.save"
RECOVERABLE = (ApprovalStatus.PENDING, ApprovalStatus.EXPIRED, ApprovalStatus.DENIED,
               ApprovalStatus.FAILED)
DEFAULT_STATUSES = (ApprovalStatus.PENDING, ApprovalStatus.EXPIRED)


def parse_statuses(values: list[str] | str | None) -> list[ApprovalStatus]:
    if values is None:
        return list(DEFAULT_STATUSES)
    if isinstance(values, str):
        values = [v for v in values.split(",") if v.strip()]
    out = []
    for value in values:
        status = ApprovalStatus(value.strip())  # ValueError on an unknown status
        if status not in RECOVERABLE:
            raise ValueError(f"status {status.value!r} cannot be recovered "
                             f"(use {', '.join(s.value for s in RECOVERABLE)})")
        out.append(status)
    return list(dict.fromkeys(out))


async def lost_saves(store: Any, statuses: list[ApprovalStatus],
                     limit: int = 1000) -> list[Approval]:
    """memory.save approvals in `statuses`, oldest first."""
    found: list[Approval] = []
    for status in statuses:
        found += [a for a in await store.list_approvals(status, limit)
                  if a.action == ACTION]
    return sorted(found, key=lambda a: a.created_at)


async def already_saved(store: Any, approval: Approval) -> bool:
    name, content = approval.args.get("name"), (approval.args.get("content") or "").strip()
    if not isinstance(name, str) or not content:
        return False
    try:
        topic = await store.get_topic(name)
    except Exception:  # noqa: BLE001 - an invalid name simply isn't saved
        return False
    return topic is not None and content in topic.content


def describe(approval: Approval, saved: bool) -> dict[str, Any]:
    args = approval.args
    content = str(args.get("content") or "")
    return {"approval_id": approval.id, "status": approval.status.value,
            "thread_key": approval.thread_key, "created_at": approval.created_at.isoformat(),
            "name": args.get("name"), "description": args.get("description"),
            "kind": args.get("kind"), "links": args.get("links"),
            "content_preview": content[:300], "content_chars": len(content),
            "already_saved": saved}


async def recover_saves(gateway: Any, store: Any, *, decided_by: str,
                        ids: list[str] | None = None,
                        statuses: list[ApprovalStatus] | None = None,
                        dry_run: bool = False, notify_threads: bool = False,
                        ) -> list[dict[str, Any]]:
    """Run the lost saves now; one result per approval considered."""
    statuses = statuses or list(DEFAULT_STATUSES)
    if ids:
        approvals = []
        for approval_id in dict.fromkeys(ids):
            approval = await store.get_approval(approval_id)
            if approval is None or approval.action != ACTION:
                approvals.append(None)
                continue
            approvals.append(approval)
        missing = [i for i, a in zip(dict.fromkeys(ids), approvals) if a is None]
        candidates = sorted((a for a in approvals if a is not None), key=lambda a: a.created_at)
    else:
        missing, candidates = [], await lost_saves(store, statuses)
    results: list[dict[str, Any]] = [
        {"approval_id": i, "outcome": "not_found",
         "detail": "no memory.save approval with this id"} for i in missing]
    for approval in candidates:
        saved = await already_saved(store, approval)
        entry = describe(approval, saved)
        if approval.status not in RECOVERABLE:
            results.append({**entry, "outcome": "skipped",
                            "detail": f"status {approval.status.value} is not recoverable"})
            continue
        if saved:
            results.append({**entry, "outcome": "skipped",
                            "detail": "content already in the topic"})
            continue
        if dry_run:
            results.append({**entry, "outcome": "would_run"})
            continue
        done = await gateway.replay(approval.id, decided_by, expected=approval.status,
                                    notify_thread=notify_threads
                                    and approval.status == ApprovalStatus.PENDING)
        if done is None:
            results.append({**entry, "outcome": "skipped",
                            "detail": "approval changed meanwhile"})
            continue
        result = done.result or {}
        results.append({**entry, "outcome": "failed" if result.get("is_error") else "executed",
                        "status": done.status.value, "result": result.get("content")})
    return results
