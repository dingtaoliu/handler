"""Approvals, questions and audit HTTP API."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import Authenticator, Principal, can_see_thread
from ..types import ApprovalStatus, QuestionStatus
from .gateway import Gateway
from .questions import InvalidAnswer, QuestionNotPending, QuestionService


class DecideBody(BaseModel):
    # Accepted for compatibility; the authenticated principal is what's recorded.
    decided_by: str | None = None


class DenyBody(DecideBody):
    reason: str = ""


class AnswerBody(BaseModel):
    option_id: str | None = None
    text: str | None = None


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


def questions_router(service: QuestionService, store: Any, auth: Authenticator) -> APIRouter:
    """Anyone who can see a question's thread can answer it; the first answer wins."""
    router = APIRouter()
    user = Depends(auth.require_principal)

    async def _visible(question_id: str, principal: Principal) -> Any:
        question = await store.get_question(question_id)
        if question is None or not can_see_thread(principal, question.thread_key):
            raise HTTPException(404, "question not found")
        return question

    @router.get("/api/questions")
    async def list_questions(
        status: str | None = None,
        limit: int = 100,
        thread_key: str | None = None,
        principal: Principal = user,
    ):
        try:
            wanted = QuestionStatus(status) if status else None
        except ValueError:
            raise HTTPException(422, f"unknown status: {status}")
        limit = max(1, min(int(limit), 500))
        questions = await store.list_questions(wanted, limit, thread_key)
        return [_json(q) for q in questions if can_see_thread(principal, q.thread_key)]

    @router.get("/api/questions/{question_id}")
    async def get_question(question_id: str, principal: Principal = user):
        return _json(await _visible(question_id, principal))

    @router.post("/api/questions/{question_id}/answer")
    async def answer(question_id: str, body: AnswerBody, principal: Principal = user):
        await _visible(question_id, principal)
        try:
            answered = await service.answer(question_id, principal.identity,
                                            option_id=body.option_id, text=body.text)
        except QuestionNotPending as exc:
            raise HTTPException(409, f"question is not pending ({exc})")
        except InvalidAnswer as exc:
            raise HTTPException(422, str(exc))
        if answered is None:
            raise HTTPException(404, "question not found")
        return _json(answered)

    @router.post("/api/questions/{question_id}/cancel")
    async def cancel(question_id: str, principal: Principal = Depends(auth.require_admin)):
        await _visible(question_id, principal)
        try:
            canceled = await service.cancel(question_id, principal.identity)
        except QuestionNotPending as exc:
            raise HTTPException(409, f"question is not pending ({exc})")
        if canceled is None:
            raise HTTPException(404, "question not found")
        return _json(canceled)

    return router
