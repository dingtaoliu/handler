"""Approvals and audit HTTP API."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from enum import Enum
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..types import ApprovalStatus
from .gateway import Gateway


class DecideBody(BaseModel):
    decided_by: str


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


def approvals_router(gateway: Gateway, store: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/api/approvals")
    async def list_approvals(status: str | None = None, limit: int = 100):
        try:
            wanted = ApprovalStatus(status) if status else None
        except ValueError:
            raise HTTPException(422, f"unknown status: {status}")
        return [_json(a) for a in await store.list_approvals(wanted, limit)]

    @router.get("/api/approvals/{approval_id}")
    async def get_approval(approval_id: str):
        approval = await store.get_approval(approval_id)
        if approval is None:
            raise HTTPException(404, "approval not found")
        return _json(approval)

    @router.post("/api/approvals/{approval_id}/approve")
    async def approve(approval_id: str, body: DecideBody):
        return await _decided(
            approval_id, await gateway.approve(approval_id, body.decided_by)
        )

    @router.post("/api/approvals/{approval_id}/deny")
    async def deny(approval_id: str, body: DenyBody):
        return await _decided(
            approval_id, await gateway.deny(approval_id, body.decided_by, body.reason)
        )

    async def _decided(approval_id: str, approval: Any):
        if approval is not None:
            return _json(approval)
        if await store.get_approval(approval_id) is None:
            raise HTTPException(404, "approval not found")
        raise HTTPException(409, "approval is not pending")

    @router.get("/api/audit")
    async def audit(run_id: str | None = None, limit: int = 200):
        return [_json(e) for e in await store.list_audit(run_id, limit)]

    return router
