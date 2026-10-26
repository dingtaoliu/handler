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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from ...auth import Authenticator, Principal
from .config import PRESET_DEFAULTS, PRESETS, Mailbox, MailboxCreate, MailboxTest, MailboxUpdate

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
