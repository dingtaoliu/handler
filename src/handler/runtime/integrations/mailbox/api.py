"""Admin API for mailboxes. Secrets are accepted, never returned.

- GET    /api/mailboxes                    list (any signed-in user)
- GET    /api/mailboxes/presets?email=     host/port presets for an address
- POST   /api/mailboxes                    create (admin)
- POST   /api/mailboxes/test               test unsaved settings (admin; password required)
- GET    /api/mailboxes/{id}               one mailbox with folder sync state
- PATCH  /api/mailboxes/{id}               update; `password` replaces the stored one (admin)
- POST   /api/mailboxes/{id}/test          test the stored settings (admin)
- POST   /api/mailboxes/{id}/sync          poll now (admin)
- DELETE /api/mailboxes/{id}               delete the mailbox, its password and ingested
                                           mail; threads and runs stay (admin)

Bodies are validated by hand so a 422 never echoes the submitted values
(FastAPI's default error body includes each invalid input, password included).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from ... import documents
from ...auth import Authenticator, Principal
from .config import PRESET_DEFAULTS, PRESETS, Mailbox, MailboxCreate, MailboxTest, MailboxUpdate
from .message import parse_message_file
from .transport import MailError

logger = logging.getLogger("handler.runtime.mailbox")


def _errors(exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", "invalid"),
         "type": e.get("type", "")} for e in exc.errors(include_input=False)]})


async def _body(request: Request, model: type[BaseModel]) -> BaseModel | JSONResponse:
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "body must be JSON"})
    try:
        return model.model_validate(data if isinstance(data, dict) else {})
    except ValidationError as exc:
        return _errors(exc)


def preset_for(email: str) -> dict[str, Any] | None:
    domain = email.rpartition("@")[2].strip().lower()
    preset = PRESETS.get(domain)
    if preset is None:
        return None
    return {**PRESET_DEFAULTS, **preset, "domain": domain}


def mailboxes_router(service, mail_store, poller, auth: Authenticator) -> APIRouter:
    router = APIRouter()

    async def public(mailbox: Mailbox, *, detail: bool = False) -> dict[str, Any]:
        data = mailbox.public()
        data["thread_key_prefix"] = f"mail:{mailbox.id}:"
        if detail:
            data["sync"] = [{k: f.get(k) for k in ("folder", "uidvalidity", "last_uid",
                                                    "backfill_until_uid", "synced_at")}
                            for f in await mail_store.list_folders(mailbox.id)]
            data["messages"] = await mail_store.counts(mailbox.id)
        return data

    async def get(mailbox_id: str) -> Mailbox:
        mailbox = await mail_store.get_mailbox(mailbox_id)
        if mailbox is None:
            raise HTTPException(404, "mailbox not found")
        return mailbox

    def can_read(mailbox: Mailbox, principal: Principal) -> bool:
        return mailbox.enabled and (
            principal.is_admin
            or (principal.user_id is not None and principal.user_id == mailbox.owner_user_id)
        )

    async def readable_mailbox(mailbox_id: str, principal: Principal) -> Mailbox:
        mailbox = await get(mailbox_id)
        if not can_read(mailbox, principal):
            raise HTTPException(404, "mailbox not found")
        return mailbox

    def mailbox_summary(mailbox: Mailbox) -> dict:
        return {"id": mailbox.id, "email": mailbox.email, "status": mailbox.status,
                "last_sync_at": mailbox.last_sync_at}

    @router.get("/api/mailboxes/conversations")
    async def list_conversations(response: Response,
                                 offset: int = Query(0, ge=0),
                                 q: str = Query("", max_length=200),
                                 principal: Principal = Depends(auth.require_principal)):
        mailboxes = [m for m in await mail_store.list_mailboxes() if can_read(m, principal)]
        limit = 40
        rows = await mail_store.list_conversations(
            [m.id for m in mailboxes], limit=limit + 1, offset=offset, search=q.strip())
        response.headers["Cache-Control"] = "no-store"
        return {"mailboxes": [mailbox_summary(m) for m in mailboxes],
                "threads": rows[:limit],
                "next_offset": offset + limit if len(rows) > limit else None}

    @router.get("/api/mailboxes/{mailbox_id}/conversations/{conversation_id}")
    async def read_conversation(mailbox_id: str, conversation_id: str, response: Response,
                                offset: int = Query(0, ge=0),
                                principal: Principal = Depends(auth.require_principal)):
        mailbox = await readable_mailbox(mailbox_id, principal)
        conversation = await mail_store.get_conversation(mailbox.id, conversation_id)
        if conversation is None:
            raise HTTPException(404, "conversation not found")
        limit = 10
        rows = await mail_store.page_conversation_messages(
            mailbox.id, conversation_id, limit=limit + 1, offset=offset)
        messages = []
        for row in reversed(rows[:limit]):
            message = {key: row[key] for key in (
                "id", "subject", "from_addr", "from_name", "to_addrs", "cc_addrs",
                "date", "direction", "body_text", "body_truncated")}
            message["attachments"] = [
                {key: item[key] for key in ("index", "filename", "size")}
                for item in row.get("attachments") or []
            ]
            messages.append(message)
        response.headers["Cache-Control"] = "no-store"
        return {"mailbox": mailbox_summary(mailbox), "subject": conversation["subject"],
                "messages": messages,
                "next_offset": offset + limit if len(rows) > limit else None}

    @router.get("/api/mailboxes/{mailbox_id}/messages/{message_id}/attachments/{index}")
    async def download_attachment(mailbox_id: str, message_id: str, index: int,
                                  principal: Principal = Depends(auth.require_principal)):
        mailbox = await readable_mailbox(mailbox_id, principal)
        row = await mail_store.get_message(message_id)
        if row is None or row["account_id"] != mailbox.id:
            raise HTTPException(404, "message not found")
        meta = next((item for item in row.get("attachments") or []
                     if item.get("index") == index), None)
        if meta is None:
            raise HTTPException(404, "attachment not found")
        try:
            raw = await service.fetch_raw(mailbox, row)
            if len(raw) > service.max_message_bytes:
                raise HTTPException(413, "message is too large")
            parsed = await documents.capped_parse("mailbox", parse_message_file, raw)
        except MailError:
            raise HTTPException(502, "attachment unavailable from mail server") from None
        except documents.ReadError:
            raise HTTPException(502, "attachment could not be read") from None
        if parsed.message_id != row["message_id"] or not 0 <= index < len(parsed.attachments):
            raise HTTPException(404, "attachment not found")
        return Response(parsed.attachments[index].data, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Disposition": "attachment; filename*=UTF-8''"
                                 + quote(meta["filename"], safe="")})

    @router.get("/api/mailboxes")
    async def list_mailboxes(_p: Principal = Depends(auth.require_principal)):
        return {"mailboxes": [await public(m) for m in await mail_store.list_mailboxes()],
                "presets": sorted(PRESETS)}

    @router.get("/api/mailboxes/presets")
    async def presets(email: str = "", _p: Principal = Depends(auth.require_principal)):
        return {"preset": preset_for(email) if email else None,
                "providers": {domain: {**PRESET_DEFAULTS, **p} for domain, p in PRESETS.items()}}

    @router.post("/api/mailboxes/test")
    async def test_unsaved(request: Request, _p: Principal = Depends(auth.require_admin)):
        body = await _body(request, MailboxTest)
        if isinstance(body, JSONResponse):
            return body
        return await service.test(body, body.password)

    @router.post("/api/mailboxes", status_code=201)
    async def create(request: Request, principal: Principal = Depends(auth.require_admin)):
        body = await _body(request, MailboxCreate)
        if isinstance(body, JSONResponse):
            return body
        values = body.model_dump(exclude={"password"})
        values["username"] = values.get("username") or values["email"]
        values["label"] = values.get("label") or values["email"]
        values["owner_user_id"] = values.get("owner_user_id") or principal.user_id
        mailbox = Mailbox(**values, created_by=principal.identity)
        if not mailbox.enabled:
            mailbox.status = "disabled"
        await service.set_password(mailbox, body.password)
        await mail_store.put_mailbox(mailbox)
        logger.info("mailbox created", extra={"mailbox": mailbox.id, "email": mailbox.email,
                                               "by": principal.identity})
        if poller is not None and mailbox.enabled:
            poller.poll_soon(mailbox.id)
        return await public(mailbox)

    @router.get("/api/mailboxes/{mailbox_id}")
    async def read(mailbox_id: str, _p: Principal = Depends(auth.require_principal)):
        return await public(await get(mailbox_id), detail=True)

    @router.patch("/api/mailboxes/{mailbox_id}")
    async def update(mailbox_id: str, request: Request,
                     principal: Principal = Depends(auth.require_admin)):
        mailbox = await get(mailbox_id)
        body = await _body(request, MailboxUpdate)
        if isinstance(body, JSONResponse):
            return body
        changes = body.model_dump(exclude_unset=True, exclude={"password"})
        changes = {k: v for k, v in changes.items()
                   if v is not None or k in ("owner_user_id",)}
        connection_changed = any(k.startswith(("imap_", "smtp_")) or k in (
            "username", "allow_self_signed") for k in changes) or body.password is not None
        for key, value in changes.items():
            setattr(mailbox, key, value)
        if "imap_host" in changes or "username" in changes:
            mailbox.sent_folder = mailbox.drafts_folder = None  # detect again
        if body.password is not None:
            await service.set_password(mailbox, body.password)
        if "enabled" in changes:
            mailbox.status = "active" if mailbox.enabled else "disabled"
        elif connection_changed and mailbox.status in ("auth_failed", "error"):
            mailbox.status = "active"
        await mail_store.put_mailbox(mailbox)
        logger.info("mailbox updated", extra={
            "mailbox": mailbox.id, "fields": sorted(changes) + (
                ["password"] if body.password is not None else []),
            "by": principal.identity})
        if poller is not None and mailbox.enabled:
            poller.poll_soon(mailbox.id)
        return await public(mailbox)

    @router.post("/api/mailboxes/{mailbox_id}/test")
    async def test_saved(mailbox_id: str, _p: Principal = Depends(auth.require_admin)):
        return await service.test(await get(mailbox_id))

    @router.post("/api/mailboxes/{mailbox_id}/sync")
    async def sync_now(mailbox_id: str, _p: Principal = Depends(auth.require_admin)):
        mailbox = await get(mailbox_id)
        if not mailbox.enabled:
            raise HTTPException(409, "mailbox is disabled")
        if poller is not None:
            poller.poll_soon(mailbox.id)
        return {"status": "scheduled"}

    @router.delete("/api/mailboxes/{mailbox_id}")
    async def delete(mailbox_id: str, principal: Principal = Depends(auth.require_admin)):
        mailbox = await get(mailbox_id)
        await service.delete(mailbox)
        logger.info("mailbox deleted", extra={"mailbox": mailbox.id, "by": principal.identity})
        return {"deleted": True}

    return router
