"""`POST /api/threads/import`: another system's finished AI chat as a chat thread.

Crontal calls it once per legacy built-in-agent conversation when it moves a
merchant onto Handler. The caller holds the bare instance admin key: an import
writes into a user's own history, so no user token (admin or not) may do it, and
the admin key acting for a user (X-Handler-User) is refused too.

The thread is history only: no event is stored, so nothing routes, no run starts
and no plan input is counted. Its messages keep their timestamps, so the chat list
orders it by its last message; the owner's read mark is set at that message, so it
is not unread, and it is not pending. The owner must already exist (404
otherwise); Crontal provisions users with `POST /api/users` before importing.

When the user writes in it later, the run has no harness session yet, so the fresh
session gets the thread's last messages as a recap (`RunExecutor._stream`).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..auth import Authenticator, Principal
from ..bus import BusMessage, RunEventBus
from ..settings import HarnessSource, resolve_harness
from ..store.thread_imports import (
    CONFLICT,
    CREATED,
    ImportedMessage,
    ThreadImport,
)
from ..types import Role

MAX_MESSAGES = 2000
MAX_MESSAGE_BYTES = 100_000  # UTF-8, per message
MAX_TOTAL_BYTES = 5_000_000  # UTF-8, all messages together
MAX_TITLE_CHARS = 120  # longer titles are shortened, as thread titles are
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
SOURCE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _utc(value: datetime) -> datetime:
    """ISO times without an offset are taken as UTC (Crontal stores naive UTC)."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(
        timezone.utc)


class ImportMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1)
    created_at: datetime

    @field_validator("text")
    @classmethod
    def _text(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("text must not be blank")
        if len(text.encode()) > MAX_MESSAGE_BYTES:
            raise ValueError(f"text is over {MAX_MESSAGE_BYTES} bytes")
        return text


class ThreadImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_user_id: str = Field(min_length=1, max_length=200)
    conversation_id: str = Field(pattern=CONVERSATION_ID.pattern)
    source: str = Field(pattern=SOURCE.pattern)
    source_id: str = Field(min_length=1, max_length=200)
    title: str | None = None
    created_at: datetime
    messages: list[ImportMessage] = Field(min_length=1, max_length=MAX_MESSAGES)

    @model_validator(mode="after")
    def _total(self) -> "ThreadImportRequest":
        total = sum(len(m.text.encode()) for m in self.messages)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"messages total {total} bytes, over {MAX_TOTAL_BYTES}")
        return self


def content_hash(body: ThreadImportRequest) -> str:
    """What makes two imports the same: everything sent, times normalised to UTC."""
    canonical = {
        "owner_user_id": body.owner_user_id, "conversation_id": body.conversation_id,
        "source": body.source, "source_id": body.source_id, "title": body.title,
        "created_at": _utc(body.created_at).isoformat(),
        "messages": [[m.role, m.text, _utc(m.created_at).isoformat()] for m in body.messages],
    }
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True)
                          .encode()).hexdigest()


def build_import(body: ThreadImportRequest, harness: str) -> ThreadImport:
    """The stored form: times in UTC and never going backwards (array order is the
    order), the thread no later than its first message, the title cleaned."""
    messages: list[ImportedMessage] = []
    for m in body.messages:
        at = _utc(m.created_at)
        if messages and at < messages[-1].created_at:
            at = messages[-1].created_at  # keep the given order; ties keep insert order
        messages.append(ImportedMessage(Role(m.role), m.text, at))
    title = " ".join((body.title or "").split())[:MAX_TITLE_CHARS] or None
    return ThreadImport(
        key=f"chat:{body.owner_user_id}:{body.conversation_id}",
        owner_user_id=body.owner_user_id, harness=harness, source=body.source,
        source_id=body.source_id, title=title,
        created_at=min(_utc(body.created_at), messages[0].created_at),
        messages=messages, content_hash=content_hash(body),
    )


def thread_import_router(store: Any, bus: RunEventBus, auth: Authenticator, *,
                         default_harness: HarnessSource) -> APIRouter:
    router = APIRouter()

    async def require_admin_key(
        principal: Principal = Depends(auth.require_admin),
    ) -> Principal:
        if principal.via != "admin_key" or principal.user_id is not None:
            raise HTTPException(403, "Importing chats requires the bare instance admin key")
        return principal

    @router.post("/api/threads/import")
    async def import_thread(body: ThreadImportRequest,
                            _: Principal = Depends(require_admin_key)) -> JSONResponse:
        if await store.get_user(body.owner_user_id) is None:
            raise HTTPException(404, "owner user not found")
        spec = build_import(body, await resolve_harness(default_harness))
        outcome = await store.import_thread(spec)
        if outcome.status == CONFLICT:
            raise HTTPException(409, outcome.reason)
        created = outcome.status == CREATED
        if created:
            bus.publish(BusMessage(spec.key, None, "thread_updated", {}))
        return JSONResponse({
            "thread_key": spec.key, "conversation_id": body.conversation_id,
            "messages": outcome.messages, "created": created,
        }, status_code=201 if created else 200)

    return router
