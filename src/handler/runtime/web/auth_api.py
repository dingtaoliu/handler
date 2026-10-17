"""User and token management API, plus ``/api/me``."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from ..auth import Authenticator, Principal
from ..store.sqlite_users import USER_ID, User


class CreateUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=USER_ID.pattern)
    display_name: str = Field(min_length=1, max_length=200)
    role: Literal["admin", "member"] = "member"


class UpdateUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(None, min_length=1, max_length=200)
    role: Literal["admin", "member"] | None = None
    disabled: bool | None = None


class CreateToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field("", max_length=200)


def auth_router(auth: Authenticator) -> APIRouter:
    router = APIRouter()
    users = auth.users
    admin = Depends(auth.require_admin)

    @router.get("/api/me")
    async def me(principal: Principal = Depends(auth.require_principal)) -> dict:
        user = await users.get_user(principal.user_id) if principal.user_id else None
        return {
            "user_id": principal.user_id,
            "role": principal.role,
            "via": principal.via,
            "display_name": user.display_name if user else None,
        }

    @router.post("/api/users", status_code=201)
    async def create_user(body: CreateUser, _: Principal = admin) -> dict:
        user = User(body.id, body.display_name, body.role)
        if not await users.create_user(user):
            raise HTTPException(409, "user already exists")
        return jsonable_encoder(user)

    @router.get("/api/users")
    async def list_users(_: Principal = admin) -> list:
        return jsonable_encoder(await users.list_users())

    @router.patch("/api/users/{user_id}")
    async def update_user(user_id: str, body: UpdateUser, _: Principal = admin) -> dict:
        user = await users.update_user(
            user_id, display_name=body.display_name, role=body.role, disabled=body.disabled
        )
        if user is None:
            raise HTTPException(404, "user not found")
        return jsonable_encoder(user)

    @router.post("/api/users/{user_id}/tokens", status_code=201)
    async def create_token(user_id: str, body: CreateToken, _: Principal = admin) -> dict:
        if await users.get_user(user_id) is None:
            raise HTTPException(404, "user not found")
        token_id, token = await users.create_token(user_id, body.label)
        return {"token_id": token_id, "token": token}

    @router.get("/api/users/{user_id}/tokens")
    async def list_tokens(user_id: str, _: Principal = admin) -> list:
        if await users.get_user(user_id) is None:
            raise HTTPException(404, "user not found")
        return jsonable_encoder(await users.list_tokens(user_id))

    @router.delete("/api/tokens/{token_id}")
    async def revoke_token(token_id: str, _: Principal = admin) -> dict:
        if not await users.revoke_token(token_id):
            raise HTTPException(404, "token not found or already revoked")
        return {"revoked": True}

    return router
