"""Tasks API: background (non-chat) runs for debugging and visibility.

GET /api/tasks              one page of tasks, newest first
GET /api/tasks/stream       SSE of `task_updated` messages only
GET /api/tasks/{run_id}     one task; admins also get its `steps` debug trace

Tasks are private to the durable owner of their thread. The step trace (tool
arguments and outputs, model calls, errors, approvals) is available only when
that owner also has the admin role.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from ..auth import Authenticator, Principal, can_access_thread
from ..bus import RunEventBus
from ..tasks import TASK_UPDATED, get_task, list_tasks
from .app import sse_stream


def _split(value: str | None) -> list[str] | None:
    parts = [p.strip() for p in (value or "").split(",") if p.strip()]
    return parts or None


def tasks_router(store: Any, bus: RunEventBus, auth: Authenticator) -> APIRouter:
    router = APIRouter()
    principal_dep = Depends(auth.require_principal)
    sse_dep = Depends(auth.require_sse_principal)

    @router.get("/api/tasks")
    async def tasks(type: str | None = None, status: str | None = None,
                    since: datetime | None = None, cursor: str | None = None,
                    limit: int = Query(50, ge=1, le=200),
                    principal: Principal = principal_dep) -> Any:
        try:
            page = await list_tasks(store, user_id=principal.user_id, types=_split(type),
                                    statuses=_split(status), since=since, cursor=cursor,
                                    limit=limit)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        visible = []
        for item in page["items"]:
            if await can_access_thread(store, principal, item["thread_key"]):
                visible.append(item)
        page["items"] = visible
        return jsonable_encoder(page)

    # Before the {run_id} route so "stream" is not taken for a run id.
    @router.get("/api/tasks/stream")
    async def task_stream(request: Request,
                          principal: Principal = sse_dep) -> StreamingResponse:
        async def authorized() -> bool:
            try:
                return await auth.require_sse_principal(request) == principal
            except HTTPException:
                return False

        return StreamingResponse(
            sse_stream(bus, "*", request,
                       visible=lambda k: can_access_thread(store, principal, k),
                       authorized=authorized, accept=lambda m: m.type == TASK_UPDATED),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/api/tasks/{run_id}")
    async def task(run_id: str, principal: Principal = principal_dep) -> Any:
        item = await get_task(store, run_id, user_id=principal.user_id,
                              include_steps=principal.is_admin)
        if item is None or not await can_access_thread(store, principal, item["thread_key"]):
            raise HTTPException(404, "task not found")
        return jsonable_encoder(item)

    return router
