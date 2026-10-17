"""Runtime settings and notifications endpoints."""

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, ValidationError

from ..auth import Authenticator, Principal
from ..settings import MODELS, SettingsConflict, SettingsService


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    updated_by: str | None = None  # accepted for compatibility; the principal is recorded


def settings_router(service: SettingsService, auth: Authenticator) -> APIRouter:
    router = APIRouter()
    user = Depends(auth.require_principal)
    admin = Depends(auth.require_admin)

    def check(section: str) -> None:
        if section not in MODELS:
            raise HTTPException(404, "Unknown settings section")

    async def section_value(section: str) -> dict:
        value, version = await service.get(section)
        return {"value": value, "version": version, "schema": service.schemas()[section]}

    @router.get("/api/settings")
    async def all_settings(_: Principal = user) -> dict:
        return {section: await section_value(section) for section in MODELS}

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
