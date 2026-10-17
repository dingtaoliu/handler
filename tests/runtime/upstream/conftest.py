import asyncio
import socket
from contextlib import asynccontextmanager

import mcp.types as mt
import pytest
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import request_ctx
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.responses import JSONResponse

from handler.runtime.types import Connection


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeUpstream:
    """Upstream MCP server with three tools; authenticates via X-Api-Key."""

    def __init__(self, keys: set[str]) -> None:
        self.keys = keys
        self.calls: list[tuple[str, dict, str]] = []
        self.manager = StreamableHTTPSessionManager(self._server(), json_response=True, stateless=True)

    def _server(self) -> Server:
        server: Server = Server("fake-upstream")
        schema = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                  "required": ["a", "b"]}

        @server.list_tools()
        async def list_tools():
            return [
                mt.Tool(name="add", description="Add two numbers", inputSchema=schema),
                mt.Tool(name="whoami", description="Echo the caller key",
                        inputSchema={"type": "object", "properties": {}}),
                mt.Tool(name="dangerous", description="Not curated",
                        inputSchema={"type": "object", "properties": {}}),
            ]

        @server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            key = request_ctx.get().request.headers.get("x-api-key", "")
            self.calls.append((name, arguments, key))
            if name == "add":
                text = str(arguments["a"] + arguments["b"])
            elif name == "whoami":
                text = key
            else:
                return mt.CallToolResult(
                    content=[mt.TextContent(type="text", text="nope")], isError=True)
            return mt.CallToolResult(content=[mt.TextContent(type="text", text=text)])

        return server

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            message = await receive()
            async with self.manager.run():
                await send({"type": "lifespan.startup.complete"})
                await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        if headers.get("x-api-key") not in self.keys:
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)


@asynccontextmanager
async def serve(app):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def upstream():
    fake = FakeUpstream({"key-alice", "key-bob", "key-inst"})
    async with serve(fake) as url:
        fake.url = url
        yield fake


class FakeConnections:
    """In-memory ConnectionStore."""

    def __init__(self) -> None:
        self.items: dict[str, Connection] = {}

    async def put_connection(self, connection):
        self.items[connection.id] = connection

    async def get_connection(self, connection_id):
        return self.items.get(connection_id)

    async def find_connection(self, connector, owner):
        for c in self.items.values():
            if c.connector == connector and c.owner == owner and c.status == "active":
                return c
        return None

    async def list_connections(self, owner=None):
        return [c for c in self.items.values() if owner is None or c.owner == owner]


@pytest.fixture
def connections():
    return FakeConnections()


@pytest.fixture
async def store_gateway(tmp_path):
    from handler.runtime.gateway import Gateway, GrantPolicy
    from handler.runtime.store.sqlite import SQLiteCore
    from handler.runtime.store.sqlite_gateway import ApprovalMixin, AuditMixin

    class GatewayStore(ApprovalMixin, AuditMixin, SQLiteCore):
        pass

    async def emit(event):
        pass

    return Gateway(GatewayStore(tmp_path / "gw.db"), GrantPolicy({}), emit_event=emit)
