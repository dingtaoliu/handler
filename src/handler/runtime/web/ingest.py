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
from ..tenancy.limits import OverLimit
from ..types import Event, Lane, ThreadSpec, ThreadType

logger = logging.getLogger("handler.runtime.ingest")
MERCHANT_SCOPE = "merchant"  # one event for all of a merchant's users

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


Harness = Callable[[], Awaitable[str]] | str


def crontal_ingest_router(
    queue: EventQueue,
    secret: str,
    *,
    harness: Harness = "codex",
) -> APIRouter:
    """POST /ingest/crontal: Crontal notification → crontal.notification event."""
    return crontal_ingest_router_for(lambda: (queue, secret, harness))


def crontal_ingest_router_for(
    binding: Callable[[], tuple[EventQueue, str, Harness]],
) -> APIRouter:
    """`/ingest/crontal` for the tenant `binding()` returns, read once per request:
    its queue, webhook secret and harness source."""
    router = APIRouter()

    @router.post("/ingest/crontal")
    async def ingest(request: Request):
        queue, secret, harness = binding()
        body = await request.body()
        if not verify(secret, request.headers.get("x-crontal-signature"), body):
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            note = json.loads(body)
            note_id = str(note["id"])
            merchant_wide = note.get("scope") == MERCHANT_SCOPE
            if note.get("scope") is not None and not merchant_wide:
                raise ValueError(f"unknown scope {note['scope']!r}")
            # A merchant-wide event is one input for the whole merchant, owned by no one
            # (shared threads are visible to every user); a note is one per recipient.
            recipient = None if merchant_wide else str(note["recipient_user_id"])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(status_code=422, detail="invalid notification body")
        entity_type, entity_id = note.get("entity_type"), note.get("entity_id")
        if entity_type and entity_id is not None:
            key = f"crontal:{entity_type}:{entity_id}"
        else:  # merchant event ids and note ids are separate sequences: keep them apart
            key = f"crontal:notification:{'merchant-' if merchant_wide else ''}{note_id}"
        harness_name = harness if isinstance(harness, str) else await harness()
        payload = {k: note.get(k) for k in ("id", "type", "entity_type", "entity_id",
                                             "data", "created_at")}
        if merchant_wide:
            payload["scope"] = MERCHANT_SCOPE
        event = Event(
            type="crontal.notification", source="crontal", payload=payload,
            idempotency_key=f"crontal:merchant:{note_id}" if merchant_wide else f"crontal:{note_id}",
            thread_key=key, user_id=recipient,
        )
        spec = ThreadSpec(key=key, type=ThreadType.CRONTAL, lane=Lane.BACKGROUND,
                          owner_user_id=recipient, harness=harness_name)
        new = await queue.put(event, spec)
        logger.info("crontal notification", extra={"notification_id": note_id,
                                                   "scope": MERCHANT_SCOPE if merchant_wide else "user",
                                                   "thread_key": key, "duplicate": not new})
        held = ({"held": True} if new.action == "held" else {"over_limit": True}) \
            if isinstance(new, OverLimit) else {}
        return JSONResponse({"thread_key": key, "duplicate": not new, **held},
                            status_code=202 if new else 200)

    return router
