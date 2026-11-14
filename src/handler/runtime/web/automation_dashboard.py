"""Read-only, instance-scoped automation dashboard, including retained history."""
from datetime import timedelta
from typing import Protocol

from fastapi import APIRouter, Depends, Query

from ..auth import Authenticator, Principal
from ..settings import SettingsService
from ..store.base import ScheduleStore, Store
from ..types import utcnow

COUNT_FIELDS = (
    "runs", "completed", "failed", "running", "cancelled", "unpriced_runs",
    "recorded_model_requests", "input_tokens", "output_tokens",
)


class AutomationStore(Store, ScheduleStore, Protocol):
    pass


def automation_dashboard_router(store: AutomationStore, auth: Authenticator, settings: SettingsService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/automations/dashboard")
    async def dashboard(days: int | None = Query(None, ge=1, le=3650),
                        _: Principal = Depends(auth.require_principal)) -> dict:
        now = utcnow()
        since = now - timedelta(days=days) if days is not None else None
        metrics = {row["thread_key"]: row for row in await store.automation_metrics(since)}
        schedules = await store.list_schedules(include_disabled=True)
        threads, _version = await settings.get("threads")
        default = threads.get("automation") or {}
        rows = []
        for schedule in schedules:
            key = f"automation:{schedule.id}"
            usage = metrics.pop(key, {})
            effective = await settings.resolve_thread(
                default, harness=usage.get("thread_harness") or schedule.harness,
                thread_model=usage.get("thread_model") or schedule.model,
            )
            rows.append({
                "id": schedule.id, "thread_key": key, "name": schedule.name,
                "prompt": schedule.prompt, "schedule": schedule.spec,
                "timezone": schedule.timezone, "enabled": schedule.enabled,
                "one_shot": schedule.one_shot, "deleted": False,
                "next_run_at": schedule.next_run_at if schedule.enabled else None,
                "last_fired_at": schedule.last_run_at, "created_at": schedule.created_at,
                "effective_model": effective["model"], "harness": effective["harness"],
                "metrics": _metrics(usage),
            })
        # Deleting a schedule must not hide its recorded spend.
        for key, usage in metrics.items():
            rows.append({
                "id": key.removeprefix("automation:"), "thread_key": key,
                "name": None, "prompt": None, "schedule": None, "timezone": None,
                "enabled": False, "one_shot": False, "deleted": True,
                "next_run_at": None, "last_fired_at": None, "created_at": None,
                "effective_model": None, "harness": usage.get("thread_harness"),
                "metrics": _metrics(usage),
            })
        rows.sort(key=lambda row: (-row["metrics"]["known_cost_usd"], row["name"] or row["id"]))
        totals = {field: sum(row["metrics"][field] for row in rows) for field in COUNT_FIELDS}
        totals["known_cost_usd"] = sum(row["metrics"]["known_cost_usd"] for row in rows)
        totals.update(automations=len(schedules), enabled=sum(s.enabled for s in schedules),
                      deleted=len(rows) - len(schedules))
        return {"items": rows, "totals": totals, "since": since, "as_of": now,
                "cost_basis": "recorded_run_totals_with_request_ledger_fallback",
                "currency": "USD"}

    return router


def _metrics(row: dict) -> dict:
    return {**{field: int(row.get(field) or 0) for field in COUNT_FIELDS},
            "known_cost_usd": float(row.get("known_cost_usd") or 0),
            **{field: row.get(field) for field in
               ("first_run_at", "last_run_at", "last_finished_at", "last_run_status", "last_run_model")}}
