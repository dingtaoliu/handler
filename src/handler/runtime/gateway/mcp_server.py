"""MCP server (streamable HTTP, stateless) in front of the Gateway.

Mount `create_mcp_app(...)` at `/mcp` and run its lifespan from the host app:

    mcp_app = create_mcp_app(gateway, tokens)

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.lifespan():
            yield

    app = FastAPI(lifespan=lifespan)
    app.mount("/mcp", mcp_app)

Starlette's Mount does not forward lifespan events, so the host must enter
`mcp_app.lifespan()` itself. Standalone (e.g. `uvicorn` on the app directly) the
app also handles the ASGI lifespan scope on its own.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import mcp.types as mt
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import request_ctx
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from ..context import RunContext, current_tenant
from .gateway import Gateway, result_text
from .tokens import RunTokens


def _bearer(headers: Any) -> str | None:
    value = headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


class McpApp:
    """ASGI app: bearer-token auth in front of the MCP streamable HTTP manager."""

    def __init__(self, gateway: Gateway | None, tokens: RunTokens) -> None:
        self._gateway = gateway
        self._tokens = tokens
        self._server = self._build_server()
        self._manager = StreamableHTTPSessionManager(
            self._server, json_response=True, stateless=True
        )

    def _build_server(self) -> Server:
        server: Server = Server("handler-gateway")
        tokens = self._tokens

        def caller(request) -> tuple[RunContext, Gateway] | None:
            """The token's run and its own tenant's gateway (the host app's tokens
            carry it; a tenant's own app falls back to its gateway)."""
            found = tokens.resolve_with_gateway(_bearer(request.headers)) if request else None
            if found is None:
                return None
            ctx, gateway = found
            gateway = gateway or self._gateway
            return (ctx, gateway) if gateway is not None else None

        @server.list_tools()
        async def list_tools() -> list[mt.Tool]:
            found = caller(request_ctx.get().request)
            if found is None:
                return []
            ctx, gateway = found
            return [
                mt.Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
                for s in await gateway.specs_for(ctx)
            ]

        @server.call_tool(validate_input=False)  # the gateway validates
        async def call_tool(name: str, arguments: dict[str, Any]) -> mt.CallToolResult:
            found = caller(request_ctx.get().request)
            if found is None:
                return mt.CallToolResult(
                    content=[mt.TextContent(type="text", text="Unauthorized")],
                    isError=True,
                )
            ctx, gateway = found
            result = await gateway.call(ctx, name, arguments)
            return mt.CallToolResult(
                content=[mt.TextContent(type="text", text=result_text(result))],
                isError=result.is_error,
            )

        return server

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Run the session manager; enter once per process from the host app."""
        async with self._manager.run():
            yield

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._serve_lifespan(receive, send)
            return
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            token = _bearer(headers)
            issuer, here = self._tokens.tenant_of(token), current_tenant()
            if self._tokens.resolve(token) is None or (issuer and here and issuer != here):
                response = JSONResponse(
                    {"error": "unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self._manager.handle_request(scope, receive, send)

    async def _serve_lifespan(self, receive: Receive, send: Send) -> None:
        message = await receive()
        if message["type"] != "lifespan.startup":
            return
        try:
            async with self.lifespan():
                await send({"type": "lifespan.startup.complete"})
                await receive()  # lifespan.shutdown
        except Exception as exc:
            await send({"type": "lifespan.startup.failed", "message": str(exc)})
            return
        await send({"type": "lifespan.shutdown.complete"})


def create_mcp_app(gateway: Gateway | None, tokens: RunTokens) -> McpApp:
    """None: the host's one MCP app, each token naming its tenant's gateway."""
    return McpApp(gateway, tokens)
