"""The platform API: Crontal provisions merchants' tenants here, without a restart.

Authenticated by HANDLER_PLATFORM_KEY alone, which is neither a tenant admin key nor
ever part of a tenant's environment. Secrets go in and never come back, except a
generated admin key, returned once when the tenant is created.
"""

from __future__ import annotations

import hmac
import secrets as token_source
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..settings import HarnessName, HarnessNotAllowed, check_harness
from .host import TenantHost
from .registry import TenantRegistry

# What a platform caller may set; anything else about a tenant is fixed at creation.
CONFIG_KEYS = frozenset({
    "HANDLER_RT_MODEL", "HANDLER_RT_EFFORT", "HANDLER_DEFAULT_HARNESS",
    "HANDLER_MAX_CONCURRENT", "HANDLER_PUBLIC_URL", "HANDLER_RETURN_TO_ORIGINS",
    "HANDLER_RT_USERS", "CRONTAL_URL",
})
SECRET_KEYS = frozenset({
    "HANDLER_ADMIN_KEY", "HANDLER_WEBHOOK_SECRET", "HANDLER_EMAIL_SECRET",
    "HANDLER_SECRET_KEY", "CRONTAL_AGENT_TOKEN", "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
})


class TenantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    limits: dict[str, Any] | None = None
    integrations: list[str] | None = None
    config: dict[str, str | None] = Field(default_factory=dict)
    secrets: dict[str, str | None] = Field(default_factory=dict)


def platform_router(registry: TenantRegistry, host: TenantHost, platform_key: str, *,
                    allowed_harnesses: frozenset[str] | None = None) -> APIRouter:
    router = APIRouter(prefix="/platform")

    def check(authorization: str | None) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), platform_key.encode()):
            raise HTTPException(401, "invalid platform key")

    async def known(merchant_id: int):
        tenant = await registry.get_by_merchant(merchant_id)
        if tenant is None:
            raise HTTPException(404, "tenant not found")
        return tenant

    @router.get("/tenants")
    async def list_tenants(authorization: str | None = Header(None)):
        check(authorization)
        return [tenant.describe() for tenant in await registry.list()]

    @router.get("/tenants/{merchant_id}")
    async def get_tenant(merchant_id: int, authorization: str | None = Header(None)):
        check(authorization)
        return (await known(merchant_id)).describe()

    @router.put("/tenants/{merchant_id}")
    async def put_tenant(merchant_id: int, body: TenantBody,
                         authorization: str | None = Header(None)):
        """Create the merchant's tenant, or change the supplied fields. Safe to repeat."""
        check(authorization)
        if merchant_id < 1:
            raise HTTPException(422, "merchant id must be positive")
        for supplied, allowed, label in ((body.config, CONFIG_KEYS, "config"),
                                         (body.secrets, SECRET_KEYS, "secrets")):
            rejected = sorted(set(supplied) - allowed)
            if rejected:
                raise HTTPException(422, f"{label} keys not allowed: {', '.join(rejected)}")
        harness = body.config.get("HANDLER_DEFAULT_HARNESS")
        if harness is not None:
            if harness not in {h.value for h in HarnessName}:
                raise HTTPException(422, f"unknown harness {harness!r}")
            try:
                check_harness(harness, allowed_harnesses, "HANDLER_DEFAULT_HARNESS")
            except HarnessNotAllowed as exc:
                raise HTTPException(422, str(exc)) from exc
        changes = {key: value for key, value in (("plan", body.plan), ("limits", body.limits),
                                                 ("integrations", body.integrations))
                   if value is not None}
        if body.config:
            changes["config"] = body.config
        try:
            tenant, created, admin_key = await registry.upsert_merchant(
                merchant_id, changes, body.secrets, lambda: token_source.token_urlsafe(32))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        await host.tenant_changed(tenant.name)
        payload = tenant.describe()
        if admin_key is not None:
            payload["admin_key"] = admin_key
        return JSONResponse(_jsonable(payload), status_code=201 if created else 200)

    async def set_enabled(merchant_id: int, enabled: bool):
        tenant = await known(merchant_id)
        if tenant.enabled != enabled:
            await registry.set_enabled(tenant.name, enabled)
            await host.tenant_changed(tenant.name)
        return (await known(merchant_id)).describe()

    @router.post("/tenants/{merchant_id}/disable")
    async def disable(merchant_id: int, authorization: str | None = Header(None)):
        check(authorization)
        return await set_enabled(merchant_id, False)

    @router.post("/tenants/{merchant_id}/enable")
    async def enable(merchant_id: int, authorization: str | None = Header(None)):
        check(authorization)
        return await set_enabled(merchant_id, True)

    return router


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(payload)
