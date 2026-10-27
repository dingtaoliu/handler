"""Approvals and audit HTTP API."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import Authenticator, Principal, can_see_thread
from ..types import ApprovalStatus
from .gateway import Gateway


class DecideBody(BaseModel):
    # Accepted for compatibility; the authenticated principal is what's recorded.
    decided_by: str | None = None


class DenyBody(DecideBody):
    reason: str = ""


def _json(record: Any) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v) for v in value]
        return value

    return convert(asdict(record))


def approvals_router(gateway: Gateway, store: Any, auth: Authenticator) -> APIRouter:
    router = APIRouter()
    user = Depends(auth.require_principal)

    @router.get("/api/approvals")
    async def list_approvals(
        status: str | None = None,
        limit: int = 100,
        thread_key: str | None = None,
        principal: Principal = user,
    ):
        try:
            wanted = ApprovalStatus(status) if status else None
        except ValueError:
            raise HTTPException(422, f"unknown status: {status}")
        approvals = await store.list_approvals(wanted, limit, thread_key)
        return [_json(a) for a in approvals if can_see_thread(principal, a.thread_key)]

    @router.get("/api/approvals/{approval_id}")
    async def get_approval(approval_id: str, principal: Principal = user):
        approval = await store.get_approval(approval_id)
        if approval is None or not can_see_thread(principal, approval.thread_key):
            raise HTTPException(404, "approval not found")
        return _json(approval)

    @router.post("/api/approvals/{approval_id}/approve")
    async def approve(approval_id: str, body: DecideBody, principal: Principal = user):
        await _check_visible(approval_id, principal)
        return await _decided(
            approval_id, await gateway.approve(approval_id, principal.identity)
        )

    @router.post("/api/approvals/{approval_id}/deny")
    async def deny(approval_id: str, body: DenyBody, principal: Principal = user):
        await _check_visible(approval_id, principal)
        return await _decided(
            approval_id, await gateway.deny(approval_id, principal.identity, body.reason)
        )

    async def _check_visible(approval_id: str, principal: Principal) -> None:
        approval = await store.get_approval(approval_id)
        if approval is not None and not can_see_thread(principal, approval.thread_key):
            raise HTTPException(404, "approval not found")

    async def _decided(approval_id: str, approval: Any):
        if approval is not None:
            return _json(approval)
        if await store.get_approval(approval_id) is None:
            raise HTTPException(404, "approval not found")
        raise HTTPException(409, "approval is not pending")

    @router.get("/api/audit")
    async def audit(
        run_id: str | None = None, limit: int = 200,
        _: Principal = Depends(auth.require_admin),
    ):
        return [_json(e) for e in await store.list_audit(run_id, limit)]

    return router
