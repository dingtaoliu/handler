"""Runtime settings and notifications endpoints."""

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, ValidationError

from ..settings import MODELS, SettingsConflict, SettingsService


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    updated_by: str | None = None


def settings_router(service: SettingsService) -> APIRouter:
    router = APIRouter()

    def check(section: str) -> None:
        if section not in MODELS:
            raise HTTPException(404, "Unknown settings section")

    async def section_value(section: str) -> dict:
        value, version = await service.get(section)
        return {"value": value, "version": version, "schema": service.schemas()[section]}

    @router.get("/api/settings")
    async def all_settings() -> dict:
        return {section: await section_value(section) for section in MODELS}

    @router.get("/api/settings/{section}")
    async def get_settings(section: str) -> dict:
        check(section)
        return await section_value(section)

    @router.put("/api/settings/{section}")
    async def put_settings(
        section: str, value: dict = Body(...), if_match: str | None = Header(None),
        updated_by: str | None = Header(None),
    ) -> dict:
        check(section)
        try:
            expected = int(if_match.strip('"')) if if_match is not None else None
        except ValueError:
            raise HTTPException(422, "If-Match must contain an integer version")
        try:
            return jsonable_encoder(await service.put(section, value, updated_by, expected))
        except SettingsConflict as exc:
            raise HTTPException(409, str(exc))
        except ValidationError as exc:
            raise HTTPException(422, jsonable_encoder(exc.errors()))

    @router.get("/api/settings/{section}/history")
    async def history(section: str) -> list:
        check(section)
        return jsonable_encoder(await service.history(section))

    @router.post("/api/settings/{section}/rollback")
    async def rollback(section: str, body: RollbackRequest) -> dict:
        check(section)
        try:
            return jsonable_encoder(await service.rollback(section, body.version, body.updated_by))
        except SettingsConflict as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    @router.get("/api/notifications")
    async def notifications(user_id: str, unread_only: bool = False) -> list:
        return jsonable_encoder(await service.store.list_notifications(user_id, unread_only))

    @router.post("/api/notifications/{id}/read")
    async def mark_read(id: str) -> dict:
        if not await service.store.mark_read(id):
            raise HTTPException(404, "Notification not found")
        return {"read": True}

    return router
