"""Credential providers: which upstream URL and headers apply to (connector, owner)."""

from __future__ import annotations

import inspect
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from ..store.base import ConnectionStore
from ..types import Connection, utcnow


@dataclass(frozen=True)
class UpstreamAccess:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    expires_at: datetime | None = None


class NeedsReauth(Exception):
    """The owner's connection exists but must be reconnected by the user."""


class CredentialProvider(Protocol):
    async def resolve(self, connector: str, owner: str) -> UpstreamAccess | None:
        """Access for (connector, owner); None when the owner has no active connection.

        Raises NeedsReauth when the connection is known to need reconnecting.
        Providers may also implement `mark_needs_reauth(connector, owner)` and
        `discovery_access(connector)` (any usable access, for schema discovery).
        """
        ...


def _headers(spec: dict[str, Any]) -> dict[str, str]:
    """Literal `headers` plus `headers_env`: {Header: ENV_VAR | {env, prefix}}."""
    out = {k: str(v) for k, v in (spec.get("headers") or {}).items()}
    for name, source in (spec.get("headers_env") or {}).items():
        env, prefix = (source, "") if isinstance(source, str) else (
            source["env"], source.get("prefix", ""))
        value = os.environ.get(env)
        if value:
            out[name] = prefix + value
    return out


class StaticCredentials:
    """URL and headers from configuration; secrets come from named env vars.

    config = {"<connector>": {"url": ..., "headers": {...},
                              "headers_env": {"Authorization": {"env": "X", "prefix": "Bearer "}},
                              "owners": {"user:alice": {same keys, override}}}}
    An owner entry overrides the connector-level values; owners without an
    entry use the connector-level access.
    """

    def __init__(self, config: dict[str, dict[str, Any]]) -> None:
        self._config = config

    async def resolve(self, connector: str, owner: str) -> UpstreamAccess | None:
        spec = self._config.get(connector)
        if spec is None:
            return None
        override = (spec.get("owners") or {}).get(owner)
        if override is not None:
            merged = {**spec, **override}
            merged["headers"] = {**(spec.get("headers") or {}), **(override.get("headers") or {})}
            merged["headers_env"] = {
                **(spec.get("headers_env") or {}), **(override.get("headers_env") or {})}
            spec = merged
        if not spec.get("url"):
            return None
        return UpstreamAccess(url=spec["url"], headers=_headers(spec))

    async def discovery_access(self, connector: str) -> UpstreamAccess | None:
        return await self.resolve(connector, "instance")


Resolver = Callable[[Connection], Awaitable[UpstreamAccess | None] | UpstreamAccess | None]


class ConnectionCredentials:
    """Looks up the connection for (connector, owner) and dispatches on its provider."""

    def __init__(self, store: ConnectionStore, resolvers: dict[str, Resolver]) -> None:
        self._store = store
        self._resolvers = resolvers

    async def resolve(self, connector: str, owner: str) -> UpstreamAccess | None:
        connection = await self._store.find_connection(connector, owner)
        if connection is None:
            for c in await self._store.list_connections(owner):
                if c.connector == connector and c.status == "needs_reauth":
                    raise NeedsReauth(f"{connector} connection for {owner} needs reauth")
            return None
        if connection.status == "needs_reauth":
            raise NeedsReauth(f"{connector} connection for {owner} needs reauth")
        if connection.status != "active":
            return None
        return await self._access(connection)

    async def _access(self, connection: Connection) -> UpstreamAccess | None:
        resolver = self._resolvers.get(connection.provider)
        if resolver is None:
            raise LookupError(f"no resolver for provider {connection.provider!r}")
        access = resolver(connection)
        if inspect.isawaitable(access):
            access = await access
        if access is not None:
            await self._store.put_connection(replace(connection, last_used_at=utcnow()))
        return access

    async def discovery_access(self, connector: str) -> UpstreamAccess | None:
        for c in await self._store.list_connections():
            if c.connector == connector and c.status == "active":
                try:
                    access = await self._access(c)
                except LookupError:
                    continue
                if access is not None:
                    return access
        return None

    async def mark_needs_reauth(self, connector: str, owner: str) -> None:
        connection = await self._store.find_connection(connector, owner)
        if connection is not None:
            await self._store.put_connection(replace(connection, status="needs_reauth"))
