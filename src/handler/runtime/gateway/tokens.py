"""In-memory run tokens: the bearer credential a harness uses against /mcp."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..context import RunContext
from ..types import utcnow


class RunTokens:
    """One per host: a token carries its run and the gateway it may call (its tenant's),
    so one MCP app serves every tenant and a call reaches only its own tenant."""

    def __init__(self, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._clock = clock
        self._tokens: dict[str, tuple[RunContext, datetime, Any]] = {}

    def issue(self, ctx: RunContext, ttl_seconds: float, gateway: Any = None) -> str:
        self._purge()
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (ctx, self._clock() + timedelta(seconds=ttl_seconds), gateway)
        return token

    def resolve(self, token: str | None) -> RunContext | None:
        found = self.resolve_with_gateway(token)
        return found[0] if found is not None else None

    def resolve_with_gateway(self, token: str | None) -> tuple[RunContext, Any] | None:
        entry = self._tokens.get(token or "")
        if entry is None:
            return None
        ctx, expires, gateway = entry
        if expires <= self._clock():
            self._tokens.pop(token, None)
            return None
        return ctx, gateway

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    def _purge(self) -> None:
        now = self._clock()
        for token in [t for t, (_, exp, _g) in self._tokens.items() if exp <= now]:
            del self._tokens[token]
