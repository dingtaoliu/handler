"""Proxy a curated subset of an upstream MCP server's tools through the gateway."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import mcp.types as mt
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ..context import RunContext
from ..types import Risk
from .base import ToolResult, ToolSpec
from .credentials import CredentialProvider, NeedsReauth, UpstreamAccess

log = logging.getLogger(__name__)


class UpstreamUnauthorized(Exception):
    """The upstream answered 401."""


def default_owner(ctx: RunContext) -> str:
    acting = ctx.acting_as or ctx.user_id
    return f"user:{acting}" if acting else "instance"


def _content_text(result: mt.CallToolResult) -> str:
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, mt.TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{getattr(block, 'type', 'content')} content omitted]")
    if not parts and result.structuredContent is not None:
        import json

        return json.dumps(result.structuredContent, default=str)
    return "\n".join(parts)


@asynccontextmanager
async def _session(access: UpstreamAccess, timeout: float, seen: list[int]):
    """An initialized client session; records HTTP statuses into `seen`."""

    async def record(response: httpx.Response) -> None:
        seen.append(response.status_code)

    async with httpx.AsyncClient(
        headers=access.headers,
        timeout=httpx.Timeout(timeout, read=timeout),
        follow_redirects=True,
        event_hooks={"response": [record]},
    ) as client:
        async with streamable_http_client(access.url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


class UpstreamConnector:
    """A ToolProvider exposing curated tools of one upstream MCP server.

    tools: {upstream_tool_name: {"risk": "read"|..., "description"?: str, "rename"?: str}}
    Call `await start()` before `tools()`: it fetches schemas once and fails on
    curated names the upstream doesn't have.
    """

    def __init__(
        self,
        name: str,
        credentials: CredentialProvider,
        tools: dict[str, dict[str, Any]],
        owner_for: Callable[[RunContext], str] = default_owner,
        *,
        timeout_seconds: float = 60,
    ) -> None:
        self.name = name
        self._credentials = credentials
        self._curated = tools
        self._owner_for = owner_for
        self._timeout = timeout_seconds
        self._specs: list[ToolSpec] | None = None

    async def start(self, access: UpstreamAccess | None = None) -> None:
        if access is None:
            discover = getattr(self._credentials, "discovery_access", None)
            access = (await discover(self.name) if discover
                      else await self._credentials.resolve(self.name, "instance"))
        if access is None:
            raise RuntimeError(f"connector {self.name!r}: no credential available to list tools")
        async with _session(access, self._timeout, []) as session:
            listed = await session.list_tools()
        upstream = {t.name: t for t in listed.tools}
        missing = sorted(set(self._curated) - set(upstream))
        if missing:
            raise ValueError(f"connector {self.name!r}: upstream has no tool(s) {missing}")
        self._specs = [self._spec(n, upstream[n], cfg) for n, cfg in self._curated.items()]

    def _spec(self, upstream_name: str, tool: mt.Tool, cfg: dict[str, Any]) -> ToolSpec:
        public = cfg.get("rename") or upstream_name

        async def handler(ctx: RunContext, args: dict[str, Any]) -> ToolResult:
            return await self._call(ctx, upstream_name, args)

        return ToolSpec(
            name=f"{self.name}_{public}",
            action=f"{self.name}.{public}",
            risk=Risk(cfg["risk"]),
            description=cfg.get("description") or tool.description or public,
            input_schema=tool.inputSchema or {"type": "object", "properties": {}},
            handler=handler,
        )

    def tools(self) -> list[ToolSpec]:
        if self._specs is None:
            raise RuntimeError(f"connector {self.name!r} not started; await start() first")
        return list(self._specs)

    async def _call(self, ctx: RunContext, tool: str, args: dict[str, Any]) -> ToolResult:
        owner = self._owner_for(ctx)
        try:
            access = await self._credentials.resolve(self.name, owner)
        except NeedsReauth:
            return self._reauth_result(owner)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"Could not resolve {self.name} credentials: {exc}", is_error=True)
        if access is None:
            return ToolResult(
                f"No active {self.name} connection for {owner}. "
                f"Ask the user to connect their {self.name} account.",
                is_error=True,
            )
        seen: list[int] = []
        try:
            async with _session(access, self._timeout, seen) as session:
                result = await session.call_tool(tool, args)
        except (Exception, BaseExceptionGroup) as exc:  # cancellation still propagates
            if 401 in seen or _is_unauthorized(exc):
                return await self._mark_reauth(owner)
            log.warning("upstream %s.%s failed: %r", self.name, tool, exc)
            return ToolResult(f"{self.name} upstream error: {_describe(exc)}", is_error=True)
        if 401 in seen:
            return await self._mark_reauth(owner)
        return ToolResult(_content_text(result), is_error=bool(result.isError))

    async def _mark_reauth(self, owner: str) -> ToolResult:
        mark = getattr(self._credentials, "mark_needs_reauth", None)
        if mark is not None:
            try:
                await mark(self.name, owner)
            except Exception:  # noqa: BLE001
                log.exception("could not mark %s/%s needs_reauth", self.name, owner)
        return self._reauth_result(owner)

    def _reauth_result(self, owner: str) -> ToolResult:
        return ToolResult(
            f"The {self.name} connection for {owner} is no longer authorized. "
            f"The user must reconnect their {self.name} account.",
            is_error=True,
        )


def _is_unauthorized(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 401
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_unauthorized(e) for e in exc.exceptions)
    return False


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"
