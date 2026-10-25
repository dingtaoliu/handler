"""User and token management API, ``/api/me`` and one-click sign-in.

One-click sign-in (used by Crontal's "Open Handler console"):

1. A trusted caller holding the instance admin key (bare, not acting as a
   user) calls ``POST /api/auth/sign-in-tickets`` with the user it vouches
   for. The user is created or brought in line (display name, role), and a
   random single-use ticket is returned. Only its hash is stored, bound to
   this instance, valid for ``TICKET_TTL_SECONDS``.
2. The browser opens ``<base>/login#ticket=<ticket>``. The fragment never
   reaches a server or a Referer; the page strips it from the address bar and
   history, then ``POST /login/ticket`` exchanges it once for an ordinary user
   token that expires after ``SESSION_TTL_SECONDS``.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth import Authenticator, Principal
from ..store.sqlite_users import USER_ID, User

TICKET_TTL_SECONDS = 60
SESSION_TTL_SECONDS = 12 * 60 * 60
SIGN_IN_TOKEN_LABEL = "sign-in session"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


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


class CreateSignInTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(pattern=USER_ID.pattern)
    display_name: str = Field(min_length=1, max_length=200)
    role: Literal["admin", "member"] = "member"


class RedeemSignInTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: str = Field(min_length=20, max_length=200)


def ticket_hash(instance_name: str, ticket: str) -> str:
    """Stored form of a ticket, bound to the instance that issued it."""
    return hashlib.sha256(f"handler-sign-in\n{instance_name}\n{ticket}".encode()).hexdigest()


def auth_router(auth: Authenticator, instance_name: str = "") -> APIRouter:
    router = APIRouter()
    users = auth.users
    admin = Depends(auth.require_admin)

    async def require_admin_key(principal: Principal = admin) -> Principal:
        # Only the instance's own operator (Crontal holds the key) may vouch for
        # a user; an admin *user* must not be able to mint sessions for others.
        if principal.via != "admin_key" or principal.user_id is not None:
            raise HTTPException(403, "Sign-in tickets require the bare instance admin key")
        return principal

    @router.post("/api/auth/sign-in-tickets", status_code=201)
    async def create_sign_in_ticket(
        body: CreateSignInTicket, _: Principal = Depends(require_admin_key),
    ) -> JSONResponse:
        user = await users.get_user(body.user_id)
        if user is None and not await users.create_user(
            User(body.user_id, body.display_name, body.role)
        ):
            user = await users.get_user(body.user_id)  # created concurrently
        if user is not None:
            if user.disabled:
                raise HTTPException(403, "user is disabled")
            if (user.display_name, user.role) != (body.display_name, body.role):
                await users.update_user(
                    body.user_id, display_name=body.display_name, role=body.role)
        ticket = secrets.token_urlsafe(32)
        expires_at = await users.create_sign_in_ticket(
            body.user_id, ticket_hash(instance_name, ticket), TICKET_TTL_SECONDS)
        return JSONResponse(jsonable_encoder({
            "ticket": ticket, "expires_at": expires_at, "expires_in": TICKET_TTL_SECONDS,
            "user_id": body.user_id, "role": body.role, "sign_in_path": "/login",
        }), status_code=201, headers=_NO_STORE)

    @router.post("/login/ticket")
    async def redeem_sign_in_ticket(body: RedeemSignInTicket) -> JSONResponse:
        # Unauthenticated by design: the ticket is the credential.
        user_id = await users.redeem_sign_in_ticket(ticket_hash(instance_name, body.ticket))
        user = await users.get_user(user_id) if user_id else None
        if user is None or user.disabled:
            raise HTTPException(401, "Sign-in link is invalid, used or expired",
                                headers=_NO_STORE)
        _, token = await users.create_token(
            user.id, SIGN_IN_TOKEN_LABEL, ttl_seconds=SESSION_TTL_SECONDS)
        return JSONResponse({
            "token": token, "user_id": user.id, "role": user.role,
            "display_name": user.display_name, "expires_in": SESSION_TTL_SECONDS,
        }, headers=_NO_STORE)

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
