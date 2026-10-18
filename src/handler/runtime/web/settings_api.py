"""Runtime settings and notifications endpoints."""

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, ValidationError

from ..auth import Authenticator, Principal
from ..instructions import build_instructions
from ..memory import MemoryTools
from ..settings import (
    MODELS, ModelName, ReasoningEffort, SettingsConflict, SettingsService,
)
from ..types import ThreadType


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    updated_by: str | None = None  # accepted for compatibility; the principal is recorded


def settings_router(service: SettingsService, auth: Authenticator,
                    memory_tools: MemoryTools | None = None) -> APIRouter:
    router = APIRouter()
    user = Depends(auth.require_principal)
    admin = Depends(auth.require_admin)

    def check(section: str) -> None:
        if section not in MODELS:
            raise HTTPException(404, "Unknown settings section")

    async def section_value(section: str) -> dict:
        value, version = await service.get(section)
        out = {"value": value, "version": version, "schema": service.schemas()[section],
               "defaults": await service.effective(section, value)}
        if section == "threads":
            # Empty model/effort per harness: lets the form relabel "Default (...)"
            # when the harness select changes before saving.
            out["harness_defaults"] = await service.harness_fallbacks()
        return out

    @router.get("/api/settings")
    async def all_settings(_: Principal = user) -> dict:
        return {section: await section_value(section) for section in MODELS}

    @router.get("/api/settings/prompt")
    async def rendered_prompt(thread_type: str, principal: Principal = user) -> dict:
        try:
            kind = ThreadType(thread_type)
        except ValueError:
            raise HTTPException(422, f"Unknown thread_type: {thread_type}")
        # Same speaker as a live chat run (the owner's user id), so the preview
        # matches what the harness actually receives.
        speaker = principal.user_id
        text = await build_instructions(
            service, memory_tools or MemoryTools(service.store),
            speaker=speaker if kind == ThreadType.CHAT else None, thread_type=kind)
        return {"thread_type": kind.value, "prompt": text}

    @router.get("/api/chat/options")
    async def chat_options(thread_key: str | None = None, principal: Principal = user) -> dict:
        """Choices for the composer; defaults are what an empty choice runs with.

        With `thread_key` (an existing chat) the defaults follow that thread's
        own harness and model; otherwise those of a new chat.
        """
        threads, _ = await service.get("threads")
        get_thread = getattr(service.store, "get_thread", None)
        thread = await get_thread(thread_key) if thread_key and get_thread else None
        if thread is not None and not principal.is_admin and thread.owner_user_id != principal.user_id:
            thread = None
        if thread is None:
            chat = await service.resolve_thread(threads.get("chat") or {})
        else:
            chat = await service.resolve_thread(
                threads.get(str(thread.type)) or {}, harness=thread.harness,
                thread_model=thread.model)
        return {"models": [m.value for m in ModelName],
                "reasoning_efforts": [e.value for e in ReasoningEffort],
                "default_model": chat["model"],
                "default_reasoning_effort": chat["reasoning_effort"],
                "harness": chat["harness"]}

    @router.get("/api/settings/{section}")
    async def get_settings(section: str, _: Principal = user) -> dict:
        check(section)
        return await section_value(section)

    @router.put("/api/settings/{section}")
    async def put_settings(
        section: str, value: dict = Body(...), if_match: str | None = Header(None),
        principal: Principal = admin,
    ) -> dict:
        check(section)
        try:
            expected = int(if_match.strip('"')) if if_match is not None else None
        except ValueError:
            raise HTTPException(422, "If-Match must contain an integer version")
        try:
            return jsonable_encoder(await service.put(section, value, principal.identity, expected))
        except SettingsConflict as exc:
            raise HTTPException(409, str(exc))
        except ValidationError as exc:
            raise HTTPException(422, jsonable_encoder(exc.errors()))

    @router.get("/api/settings/{section}/history")
    async def history(section: str, _: Principal = user) -> list:
        check(section)
        return jsonable_encoder(await service.history(section))

    @router.post("/api/settings/{section}/rollback")
    async def rollback(
        section: str, body: RollbackRequest, principal: Principal = admin
    ) -> dict:
        check(section)
        try:
            return jsonable_encoder(await service.rollback(section, body.version, principal.identity))
        except SettingsConflict as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    @router.get("/api/notifications")
    async def notifications(
        user_id: str | None = None, unread_only: bool = False,
        principal: Principal = user,
    ) -> list:
        target = user_id or principal.user_id
        if target is None:
            raise HTTPException(400, "user_id is required")
        if target != principal.user_id and not principal.is_admin:
            raise HTTPException(403, "Cannot read another user's notifications")
        return jsonable_encoder(await service.store.list_notifications(target, unread_only))

    @router.post("/api/notifications/{id}/read")
    async def mark_read(id: str, principal: Principal = user) -> dict:
        if not principal.is_admin and principal.user_id is not None:
            own = await service.store.list_notifications(principal.user_id, False)
            if id not in {n.id for n in own}:
                raise HTTPException(404, "Notification not found")
        if not await service.store.mark_read(id):
            raise HTTPException(404, "Notification not found")
        return {"read": True}

    return router
