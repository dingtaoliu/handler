"""Persist business outcome reports independently of execution lifecycle.

Uses the stores' shared named-parameter adapter; JSON is portable text in these
new tables, avoiding backend-specific update/merge semantics.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from ..types import new_id, utcnow


class TaskOutcomeStore:
    _usage_query: Callable[..., Awaitable[Any]]

    async def bind_task_run(self, run_id: str, thread_key: str, task_id: str) -> str:
        now = utcnow().isoformat()
        await self._usage_query(
            "INSERT INTO task_outcomes(id,thread_key,updated_at) VALUES(:id,:thread,:now) "
            "ON CONFLICT(id) DO NOTHING", {"id": task_id, "thread": thread_key, "now": now})
        await self._usage_query(
            "INSERT INTO task_run_links(run_id,task_id) VALUES(:run,:task) "
            "ON CONFLICT(run_id) DO NOTHING", {"run": run_id, "task": task_id})
        task = await self.task_outcome_for_run(run_id)
        assert task is not None
        return task["id"]

    async def task_outcome_for_run(self, run_id: str) -> dict | None:
        rows = await self._usage_query(
            "SELECT t.* FROM task_outcomes t JOIN task_run_links l ON l.task_id=t.id "
            "WHERE l.run_id=:run", {"run": run_id}, fetch=True)
        if not rows:
            return None
        row = dict(rows[0])
        row["report"] = json.loads(row["report"])
        links = await self._usage_query("SELECT run_id FROM task_run_links WHERE task_id=:id "
                                        "ORDER BY run_id", {"id": row["id"]}, fetch=True)
        row["run_ids"] = [link["run_id"] for link in links]
        return row

    async def task_receipts(self, task_id: str) -> list[dict]:
        rows = await self._usage_query(
            "SELECT * FROM task_receipts WHERE task_id=:id ORDER BY created_at,id",
            {"id": task_id}, fetch=True)
        return [{**dict(row), "facts": json.loads(row["facts"])} for row in rows]

    async def add_task_receipt(self, run_id: str, thread_key: str, tool: str,
                               operation: str, status: str, facts: dict) -> str:
        task = await self.task_outcome_for_run(run_id)
        if task is None:
            await self.bind_task_run(run_id, thread_key, run_id)
            task = await self.task_outcome_for_run(run_id)
        receipt_id = new_id("receipt")
        assert task is not None
        await self._usage_query(
            "INSERT INTO task_receipts(id,task_id,run_id,tool,operation,status,facts,created_at) "
            "VALUES(:id,:task,:run,:tool,:operation,:status,:facts,:now)",
            {"id": receipt_id, "task": task["id"], "run": run_id, "tool": tool,
             "operation": operation, "status": status,
             "facts": json.dumps(facts, ensure_ascii=False).replace("\\u0000", ""),
             "now": utcnow().isoformat()})
        await self._usage_query("UPDATE task_outcomes SET updated_at=:now WHERE id=:id",
                                {"id": task["id"], "now": utcnow().isoformat()})
        return receipt_id

    async def save_task_report(self, task_id: str, report: dict) -> None:
        await self._usage_query(
            "UPDATE task_outcomes SET report=:report,updated_at=:now WHERE id=:id",
            {"id": task_id, "report": json.dumps(report, ensure_ascii=False).replace("\\u0000", ""),
             "now": utcnow().isoformat()})

    async def update_task_job(self, run_id: str, resource: str, state: str, receipt_id: str) -> None:
        task = await self.task_outcome_for_run(run_id)
        assert task is not None
        await self._usage_query(
            "INSERT INTO task_job_links(task_id,resource_key,state,receipt_id) "
            "VALUES(:task,:resource,:state,:receipt) ON CONFLICT(task_id,resource_key) "
            "DO UPDATE SET state=excluded.state,receipt_id=excluded.receipt_id",
            {"task": task["id"], "resource": resource, "state": state, "receipt": receipt_id})
        # A subsequent read of the same merchant-local RFQ updates the original
        # task too. No polling job is created and no completion is inferred.
        await self._usage_query(
            "UPDATE task_job_links SET state=:state,receipt_id=:receipt WHERE resource_key=:resource",
            {"resource": resource, "state": state, "receipt": receipt_id})
        await self._usage_query(
            "UPDATE task_outcomes SET updated_at=:now WHERE id IN "
            "(SELECT task_id FROM task_job_links WHERE resource_key=:resource)",
            {"resource": resource, "now": utcnow().isoformat()})

    async def task_jobs(self, task_id: str) -> list[dict]:
        rows = await self._usage_query(
            "SELECT resource_key,state,receipt_id FROM task_job_links WHERE task_id=:task ORDER BY resource_key",
            {"task": task_id}, fetch=True)
        return [dict(row) for row in rows]

    async def task_latest_run_status(self, task_id: str) -> str | None:
        rows = await self._usage_query(
            "SELECT r.status FROM runs r JOIN task_run_links l ON l.run_id=r.id "
            "WHERE l.task_id=:task ORDER BY r.started_at DESC,r.id DESC LIMIT 1",
            {"task": task_id}, fetch=True)
        return rows[0]["status"] if rows else None
