"""Admin endpoints to list and recover memory.save calls lost to approvals.

See memory_recovery.py. Admin key only.

    GET  /api/admin/memory-saves?status=pending,expired
    POST /api/admin/memory-saves/recover
         {"ids": [...]?, "statuses": ["pending", "expired"]?, "dry_run": false,
          "notify_threads": false}
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from ..auth import Authenticator, Principal
from ..memory_recovery import already_saved, describe, lost_saves, parse_statuses, recover_saves


class RecoverBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] | None = None
    statuses: list[str] | None = None
    dry_run: bool = False
    # Wake the requesting thread with approval.resolved (pending ones only), as
    # a normal approval would. Off by default: old threads would all run again.
    notify_threads: bool = False


def memory_recovery_router(gateway: Any, store: Any, auth: Authenticator) -> APIRouter:
    router = APIRouter()
    admin = Depends(auth.require_admin)

    def statuses(value: Any):
        try:
            return parse_statuses(value)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @router.get("/api/admin/memory-saves")
    async def list_lost(status: str | None = None, _: Principal = admin) -> dict:
        found = await lost_saves(store, statuses(status))
        return {"saves": [describe(a, await already_saved(store, a)) for a in found]}

    @router.post("/api/admin/memory-saves/recover")
    async def recover(body: RecoverBody, principal: Principal = admin) -> dict:
        results = await recover_saves(
            gateway, store, decided_by=f"{principal.identity} (memory recovery)",
            ids=body.ids, statuses=statuses(body.statuses), dry_run=body.dry_run,
            notify_threads=body.notify_threads)
        counts: dict[str, int] = {}
        for item in results:
            counts[item["outcome"]] = counts.get(item["outcome"], 0) + 1
        return {"dry_run": body.dry_run, "counts": counts, "results": results}

    return router
