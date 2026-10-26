"""The agent's own Crontal identity: the instance's AI user.

Crontal gives every Handler instance one AI user ("<merchant> AI", a member of
the merchant) and lets the instance's agent token act only as that user, never
as a person. Handler discovers it with `GET /api/v1/agent-access/identity`
(agent token) and caches it; every Crontal call (`crontal_api_*`, the curated
`crontal.*` tools) then acts as it on every thread type. The human a chat run
works for travels as `X-Crontal-On-Behalf-Of`, for Crontal's audit only.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..context import RunContext

IDENTITY_PATH = "/api/v1/agent-access/identity"
IDENTITY_TTL_SECONDS = 600
ON_BEHALF_OF_HEADER = "X-Crontal-On-Behalf-Of"


@dataclass(frozen=True)
class CrontalIdentity:
    user_id: str
    display_name: str
    merchant_id: str | None = None
    merchant_name: str | None = None
    username: str | None = None


class IdentityUnavailable(Exception):
    """Crontal did not return an AI identity. `definitive` is True when Crontal
    answered and refused (no identity for this token), False for transport or
    server errors worth retrying."""

    def __init__(self, message: str, *, definitive: bool) -> None:
        super().__init__(message)
        self.definitive = definitive


class CrontalIdentityClient:
    """Fetches and caches the AI identity for one (Crontal, agent token)."""

    def __init__(self, base_url: str, agent_token: str, *,
                 client: httpx.AsyncClient | None = None,
                 ttl_seconds: float = IDENTITY_TTL_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self._agent_token = agent_token
        self._client = client
        self._ttl = ttl_seconds
        self._cached: tuple[CrontalIdentity, float] | None = None
        self._lock = asyncio.Lock()

    @property
    def cached(self) -> CrontalIdentity | None:
        return self._cached[0] if self._cached else None

    async def get(self, *, refresh: bool = False) -> CrontalIdentity:
        if self._cached and not refresh and self._cached[1] > time.time():
            return self._cached[0]
        async with self._lock:
            if self._cached and not refresh and self._cached[1] > time.time():
                return self._cached[0]
            identity = await self._fetch()
            self._cached = (identity, time.time() + self._ttl)
            return identity

    async def _fetch(self) -> CrontalIdentity:
        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            response = await client.get(
                f"{self.base_url}{IDENTITY_PATH}",
                headers={"Authorization": f"Bearer {self._agent_token}"})
        except httpx.HTTPError as exc:
            raise IdentityUnavailable(f"Crontal unreachable: {exc}", definitive=False) from exc
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code != 200:
            raise IdentityUnavailable(
                f"Crontal refused the AI identity (HTTP {response.status_code}): "
                f"{response.text[:200]}",
                definitive=response.status_code in (401, 403, 404))
        try:
            body: dict[str, Any] = response.json()
            user_id = str(body["user_id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise IdentityUnavailable("Crontal sent no AI identity",
                                      definitive=False) from exc
        merchant_id = body.get("merchant_id")
        return CrontalIdentity(
            user_id=user_id,
            display_name=str(body.get("display_name") or body.get("username")
                             or "Crontal AI"),
            merchant_id=str(merchant_id) if merchant_id is not None else None,
            merchant_name=body.get("merchant_name"),
            username=body.get("username"),
        )


_SHARED: dict[tuple[str, str], CrontalIdentityClient] = {}


def crontal_identity(base_url: str, agent_token: str, *,
                     client: httpx.AsyncClient | None = None) -> CrontalIdentityClient:
    """The shared identity client for this Crontal and agent token (one per
    instance; instances on one host have different tokens)."""
    key = (base_url.rstrip("/"), hashlib.sha256(agent_token.encode()).hexdigest())
    if key not in _SHARED:
        _SHARED[key] = CrontalIdentityClient(base_url, agent_token, client=client)
    return _SHARED[key]


def on_behalf_of(ctx: RunContext | None) -> str | None:
    """The person a run works for, sent to Crontal for audit: the chatting
    user on chat threads only (background threads work for the merchant).
    Only Crontal user ids (digits) are sent."""
    if ctx is None or not (ctx.thread_key or "").startswith("chat:"):
        return None
    user = (ctx.acting_as or ctx.user_id or "").removeprefix("user:")
    return user if user.isdigit() else None


def identity_prompt(identity: CrontalIdentity) -> str:
    """Prompt text: who the agent is in Crontal."""
    merchant = identity.merchant_name or "your merchant"
    return (f"In Crontal you act as \"{identity.display_name}\" (Crontal user "
            f"{identity.user_id}), the AI member of {merchant}. Everything you do "
            f"in Crontal is recorded as that AI user, never as a person, with "
            f"{merchant}'s ordinary member permissions; on chat threads Crontal "
            f"also notes which person you worked for. You cannot sign in as, "
            f"invite, or manage people, or change the company profile.")
