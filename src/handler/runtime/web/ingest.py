"""Signed webhook ingress for application events (Crontal notifications)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..store.base import EventQueue
from ..types import Event, Lane, ThreadSpec, ThreadType

logger = logging.getLogger("handler.runtime.ingest")

MAX_SKEW_SECONDS = 300


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """Header value for X-Crontal-Signature (used by tests and by Crontal)."""
    t = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify(secret: str, header: str | None, body: bytes, *, now: float | None = None) -> bool:
    if not header:
        return False
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        t = int(parts["t"])
        given = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs((time.time() if now is None else now) - t) > MAX_SKEW_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


def crontal_ingest_router(
    queue: EventQueue,
    secret: str,
    *,
    harness: Callable[[], Awaitable[str]] | str = "codex",
) -> APIRouter:
    """POST /ingest/crontal: Crontal notification → crontal.notification event."""
    router = APIRouter()

    @router.post("/ingest/crontal")
    async def ingest(request: Request):
        body = await request.body()
        if not verify(secret, request.headers.get("x-crontal-signature"), body):
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            note = json.loads(body)
            note_id = str(note["id"])
            recipient = str(note["recipient_user_id"])
        except (ValueError, KeyError, TypeError):
            raise HTTPException(status_code=422, detail="invalid notification body")
        entity_type, entity_id = note.get("entity_type"), note.get("entity_id")
        key = (f"crontal:{entity_type}:{entity_id}" if entity_type and entity_id is not None
               else f"crontal:notification:{note_id}")
        harness_name = harness if isinstance(harness, str) else await harness()
        event = Event(
            type="crontal.notification", source="crontal",
            payload={k: note.get(k) for k in ("id", "type", "entity_type", "entity_id",
                                               "data", "created_at")},
            idempotency_key=f"crontal:{note_id}", thread_key=key, user_id=recipient,
        )
        spec = ThreadSpec(key=key, type=ThreadType.CRONTAL, lane=Lane.BACKGROUND,
                          owner_user_id=recipient, harness=harness_name)
        new = await queue.put(event, spec)
        logger.info("crontal notification", extra={"notification_id": note_id,
                                                   "thread": key, "duplicate": not new})
        return JSONResponse({"thread_key": key, "duplicate": not new},
                            status_code=202 if new else 200)

    return router
