"""POST /ingest/email: raw RFC 822 mail from the Cloudflare Email Worker.

Request (see deploy/cloudflare-email-worker/):
- body: the raw message bytes (``message.raw``), unchanged;
- ``X-Handler-Timestamp``: unix seconds; requests older than 5 minutes fail;
- ``X-Handler-Signature``: ``v1=<hex HMAC-SHA256(secret, "<timestamp>." + body)>``;
- ``X-Handler-Envelope-From`` / ``-To``: Cloudflare's envelope (URI-encoded);
- ``X-Handler-Auth-Results``: base64 JSON of auth headers the Worker saw
  (informational; handler verifies DKIM itself);
- ``X-Handler-Forwarded``: whether the Worker's copy to Gmail succeeded.

Durable before acknowledged: files go to the thread's durable workspace,
then the event is committed, then 2xx. The Message-ID is the idempotency key.
Mail failing the sender gate is stored as an ``ignored`` event with a reason
(visible in Activity) and never reaches the agent.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..mail.dkim import TxtResolver, doh_resolver
from ..mail.intake import (
    build_payload,
    check_sender,
    idempotency_key,
    resolve_act_as,
    save_files,
    thread_key,
)
from ..mail.parse import parse_email
from ..run import thread_slug
from ..settings import HarnessSource, SettingsService, resolve_harness
from ..store.base import Store
from ..types import Event, Lane, ThreadSpec, ThreadType
from ..workspaces import WorkspaceSync

logger = logging.getLogger("handler.runtime.ingest.email")

MAX_SKEW_SECONDS = 300
MAX_EMAIL_BYTES = 30 * 1024 * 1024  # Cloudflare Email Routing accepts up to 25 MiB


def sign_email(secret: str, body: bytes, timestamp: int | None = None) -> tuple[str, str]:
    """(X-Handler-Timestamp, X-Handler-Signature) values, as the Worker sends them."""
    t = str(int(time.time()) if timestamp is None else timestamp)
    mac = hmac.new(secret.encode(), t.encode() + b"." + body, hashlib.sha256).hexdigest()
    return t, f"v1={mac}"


def verify_email(secret: str, timestamp: str | None, signature: str | None, body: bytes,
                 *, now: float | None = None) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        t = int(timestamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - t) > MAX_SKEW_SECONDS:
        return False
    expected = hmac.new(secret.encode(), str(t).encode() + b"." + body,
                        hashlib.sha256).hexdigest()
    given = signature.strip().removeprefix("v1=")
    return hmac.compare_digest(expected, given)


def _header_auth(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        decoded = json.loads(base64.b64decode(value))
    except (ValueError, TypeError):
        return {"unparsed": value[:500]}
    return decoded if isinstance(decoded, dict) else None


def email_ingest_router(
    store: Store,
    secret: str,
    settings: SettingsService,
    workspace_sync: WorkspaceSync,
    *,
    harness: HarnessSource = "codex",
    resolver: TxtResolver | None = None,
    max_bytes: int = MAX_EMAIL_BYTES,
) -> APIRouter:
    router = APIRouter()
    dns = resolver

    @router.post("/ingest/email")
    async def ingest(request: Request):
        nonlocal dns
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise HTTPException(413, "email too large")
        body = await request.body()
        if len(body) > max_bytes:
            raise HTTPException(413, "email too large")
        if not verify_email(secret, request.headers.get("x-handler-timestamp"),
                            request.headers.get("x-handler-signature"), body):
            raise HTTPException(401, "invalid signature")
        if not body.strip():
            raise HTTPException(422, "empty email")
        try:
            parsed = await asyncio.to_thread(parse_email, body)
        except Exception as exc:  # noqa: BLE001 - the parser is lenient; be safe
            logger.warning("unparseable email", extra={"error": str(exc)})
            raise HTTPException(422, "unparseable email")

        envelope_from = unquote(request.headers.get("x-handler-envelope-from", "")) or None
        envelope_to = unquote(request.headers.get("x-handler-envelope-to", "")) or None
        header_auth = _header_auth(request.headers.get("x-handler-auth-results"))
        forwarded = request.headers.get("x-handler-forwarded") or None
        key = thread_key(parsed)
        idem = idempotency_key(parsed)
        config, _ = await settings.get("email")

        if dns is None:
            dns = doh_resolver()
        gate = await check_sender(
            body, parsed, envelope_from=envelope_from,
            allowed_senders=list(config.get("allowed_senders") or []),
            require_auth=bool(config.get("require_auth", True)), resolver=dns)
        acting = None
        reason = gate.reason
        if gate.accepted:
            acting = await resolve_act_as(store, config.get("act_as"))
            if acting is None:
                reason = "act_as_not_configured"

        common: dict[str, Any] = {"envelope_from": envelope_from, "envelope_to": envelope_to,
                                  "auth": gate.auth, "header_auth": header_auth,
                                  "forwarded_copy": forwarded}
        if reason is not None:
            payload = build_payload(parsed, ignored_reason=reason, **common)
            payload["checked_sender"] = gate.sender
            event = Event(type="email.received", source="email", payload=payload,
                          idempotency_key=idem, thread_key=key)
            new = await store.put(event, None)
            logger.info("email ignored", extra={"reason": reason, "thread_key": key,
                                                "duplicate": not new})
            return JSONResponse({"status": "ignored", "reason": reason, "thread_key": key,
                                 "duplicate": not new}, status_code=200)

        slug = thread_slug(key)

        async def write(relative: str, data: bytes) -> None:
            await workspace_sync.put_file(slug, relative, data)

        with tempfile.TemporaryDirectory(prefix="handler-email-") as scratch:
            workspace = await save_files(parsed, body, write, scratch=Path(scratch))
        payload = build_payload(parsed, workspace=workspace, **common)
        payload["accepted_sender"] = gate.sender
        event = Event(type="email.received", source="email", payload=payload,
                      idempotency_key=idem, thread_key=key, user_id=acting)
        spec = ThreadSpec(key=key, type=ThreadType.EMAIL, lane=Lane.BACKGROUND,
                          owner_user_id=acting, harness=await resolve_harness(harness))
        new = await store.put(event, spec)
        logger.info("email received", extra={"thread_key": key, "duplicate": not new,
                                             "attachments": len(parsed.attachments)})
        return JSONResponse({"status": "accepted", "thread_key": key, "duplicate": not new,
                             "workspace": workspace["dir"]},
                            status_code=202 if new else 200)

    return router
