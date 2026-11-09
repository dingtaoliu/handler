"""Receipt-backed reporting; a finished model run is not business completion."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .context import RunContext
from .types import RunStatus

OUTCOMES = ["in_progress", "awaiting_approval", "awaiting_review", "blocked", "partial",
            "completed", "canceled"]
# Only compact machine receipts, never email bodies, descriptions or model text.
_FACT_KEYS = {"id", "file_id", "draft_id", "message_id", "schedule_id", "status", "intake_status",
              "job_id", "task_id", "row_count", "column_count", "rows", "columns", "count",
              "total", "total_count", "line_count", "size", "sha256", "deleted", "reused"}


def receipt_facts(value: Any, prefix: str = "", depth: int = 0) -> dict:
    facts = {}
    if not isinstance(value, dict) or depth > 3:
        return facts
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if key in _FACT_KEYS and isinstance(item, (str, int, float, bool)):
            if not isinstance(item, str) or len(item) <= 160:
                facts[path] = item
        elif key in {"body", "data", "result", "artifact", "rfq"} and isinstance(item, dict):
            facts.update(receipt_facts(item, path, depth + 1))
        elif isinstance(item, list) and key in {"listings", "items", "line_items", "notification_ids"}:
            facts[path + ".count"] = len(item)
    return facts


def operation_key(tool: str, args: dict) -> str:
    # A corrected payload for the same operation can recover a validation error;
    # an unrelated successful call to the same tool cannot erase it.
    identity = {key: args[key] for key in ("method", "path", "message_id", "draft_id",
                "reply_to_message_id", "inquiry_key", "mailbox_id", "schedule_id") if key in args}
    if not identity:
        identity = args
    return tool + ":" + hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()


async def bind_run_task(store: Any, run: Any, events: list, thread: Any) -> None:
    if not hasattr(store, "bind_task_run"):
        return
    parents = set()
    continuation = bool(events)
    for event in events:
        obj = None
        if event.type == "approval.resolved":
            obj = await store.get_approval(event.payload.get("approval_id", ""))
        elif event.type == "question.answered":
            obj = await store.get_question(event.payload.get("question_id", ""))
        else:
            continuation = False
        parent = await store.task_outcome_for_run(obj.run_id) if obj else None
        if parent:
            parents.add(parent["id"])
        else:
            continuation = False
    # Stable over retries of the same event batch. New user turns/jobs start new
    # tasks; never merge merely because they share a thread.
    task_id = next(iter(parents)) if continuation and len(parents) == 1 else (
        "task_" + hashlib.sha256("\n".join(sorted(e.id for e in events)).encode()).hexdigest()[:24])
    await store.bind_task_run(run.id, thread.key, task_id)


async def capture_receipt(store: Any, ctx: RunContext, tool: str, args: dict,
                          status: str, content: Any = None) -> str | None:
    if tool.startswith("task_") or not hasattr(store, "add_task_receipt"):
        return None
    facts = receipt_facts(content)
    if status == "ok" and (facts.get("status") == 202 or
            any(v in ("pending", "queued", "running", "extracting") for k, v in facts.items()
                if k.endswith("status"))):
        status = "pending"
    path = str(args.get("path", "")).split("?", 1)[0]
    rfq = re.match(r"^/api/v1/rfq/received/(\d+)(?:/|$)", path)
    rid = rfq[1] if rfq else facts.get("body.id") if path.startswith("/api/v1/rfq/received") else None
    if rid is not None and any(k.endswith("intake_status") for k in facts):
        facts["resource"] = "rfq_received:" + str(rid)
    receipt_id = await store.add_task_receipt(ctx.run_id, ctx.thread_key, tool,
                                             operation_key(tool, args), status, facts)
    if facts.get("resource"):
        state = next(v for k, v in facts.items() if k.endswith("intake_status"))
        await store.update_task_job(ctx.run_id, facts["resource"], str(state), receipt_id)
    return receipt_id


async def outcome_view(store: Any, run_id: str) -> dict | None:
    task = await store.task_outcome_for_run(run_id)
    if task is None:
        return None
    receipts = await store.task_receipts(task["id"])
    jobs = await store.task_jobs(task["id"])
    latest = {row["operation"]: row for row in receipts}
    errors = [r["id"] for r in latest.values() if r["status"] in {"error", "denied"}]
    pending = []
    pending += [a["id"] for a in await store.task_approvals(task["run_ids"], pending_only=True)]
    pending += [q["id"] for q in await store.task_questions(task["run_ids"], pending_only=True)]
    report = task["report"]
    state = report.get("outcome", "in_progress")
    reason = report.get("reason", "No task outcome has been reported")
    unfinished = [j for j in jobs if j["state"] != "confirmed"]
    # Known RFQ jobs have a resource-specific resolver; unknown async receipts
    # remain unverified until the same operation produces a terminal receipt.
    async_pending = [r["id"] for r in latest.values() if r["status"] == "pending"
                     and not r["facts"].get("resource")]
    run_status = await store.task_latest_run_status(task["id"])
    if pending:
        state, reason = "awaiting_approval", "Waiting for a recorded approval or answer"
    elif errors:
        state, reason = ("partial" if any(r["status"] == "ok" for r in receipts) else "blocked"), "Unresolved tool errors"
    elif unfinished:
        failed = any(j["state"] == "extraction_failed" for j in unfinished)
        state = "blocked" if failed else "in_progress" if any(j["state"] == "extracting" for j in unfinished) else "awaiting_review"
        reason = "Linked extraction is incomplete or requires review"
    elif async_pending:
        state, reason = "awaiting_review", "An asynchronous action has no terminal receipt"
    elif state == "completed" and report.get("receipt_count") != len(receipts):
        state, reason = "awaiting_review", "New tool activity requires an updated outcome report"
    elif run_status in (RunStatus.FAILED, RunStatus.ABANDONED, RunStatus.CANCELLED):
        state, reason = "blocked", "Run was interrupted or failed"
    elif run_status == RunStatus.COMPLETED and not report:
        state, reason = "awaiting_review", "Run finished without a verified task outcome"
    return {"id": task["id"], "outcome": state, "reason": reason,
            "pending_work": report.get("pending_work", []), "checks": report.get("checks", []),
            "unresolved_error_receipt_ids": errors, "pending_ids": pending,
            "pending_receipt_ids": async_pending,
            "run_ids": task["run_ids"], "jobs": jobs, "updated_at": task["updated_at"],
            "verification": "receipt_facts_only; task scope and source coverage require agent review",
            "receipts": receipts}


async def finish_task_run(store: Any, run: Any) -> None:
    if not hasattr(store, "task_outcome_for_run"):
        return
    task = await store.task_outcome_for_run(run.id)
    if task is None:
        return
    if run.status != RunStatus.COMPLETED:
        await store.save_task_report(task["id"], {"outcome": "blocked", "reason": "Run did not finish normally",
                                                 "pending_work": ["Investigate or retry the execution"]})
    elif not task["report"] or task["report"].get("outcome") == "in_progress":
        await store.save_task_report(task["id"], {"outcome": "awaiting_review", "reason":
            "Run finished; task completion has not been verified", "pending_work": []})
