"""In-memory run tokens: the bearer credential a harness uses against /mcp."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime, timedelta

from ..context import RunContext
from ..types import utcnow


class RunTokens:
    def __init__(self, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._clock = clock
        self._tokens: dict[str, tuple[RunContext, datetime]] = {}

    def issue(self, ctx: RunContext, ttl_seconds: float) -> str:
        self._purge()
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (ctx, self._clock() + timedelta(seconds=ttl_seconds))
        return token

    def resolve(self, token: str | None) -> RunContext | None:
        entry = self._tokens.get(token or "")
        if entry is None:
            return None
        ctx, expires = entry
        if expires <= self._clock():
            self._tokens.pop(token, None)
            return None
        return ctx

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    def _purge(self) -> None:
        now = self._clock()
        for token in [t for t, (_, exp) in self._tokens.items() if exp <= now]:
            del self._tokens[token]
