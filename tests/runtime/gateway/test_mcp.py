import asyncio
import json
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from handler.runtime.context import RunContext
from handler.runtime.gateway import RunTokens, create_mcp_app

CTX = RunContext(run_id="run-mcp", thread_key="chat:u:1", user_id="u1", acting_as="u1")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def serve(app):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
def tokens():
    return RunTokens()


@pytest.fixture
def mcp_app(gateway, tokens):
    return create_mcp_app(gateway, tokens)


@asynccontextmanager
async def session(url, token):
    async with streamablehttp_client(
        url, headers={"Authorization": f"Bearer {token}"}
    ) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s


async def test_roundtrip_standalone(mcp_app, tokens, store, tools):
    token = tokens.issue(CTX, 600)
    async with serve(mcp_app) as base:  # app handles its own ASGI lifespan
        async with session(f"{base}/", token) as s:
            listed = await s.list_tools()
            names = {t.name for t in listed.tools}
            assert {"echo", "send_it"} <= names
            echo = next(t for t in listed.tools if t.name == "echo")
            assert echo.inputSchema["required"] == ["text"]

            ok = await s.call_tool("echo", {"text": "hello"})
            assert not ok.isError
            body = json.loads(ok.content[0].text)
            assert body["echo"] == "hello" and body["run"] == "run-mcp" and body["as"] == "u1"

            pending = await s.call_tool("send_it", {"text": "x"})
            assert not pending.isError
            assert json.loads(pending.content[0].text)["status"] == "pending_approval"

            bad = await s.call_tool("echo", {})
            assert bad.isError and "text" in bad.content[0].text
            denied = await s.call_tool("blocked", {"text": "a"})
            assert denied.isError and "Denied" in denied.content[0].text
            boom = await s.call_tool("boom", {"text": "a"})
            assert boom.isError
    assert len(tools.calls) == 1
    assert len(await store.list_audit("run-mcp")) == 6


async def test_mounted_under_fastapi_with_host_lifespan(mcp_app, tokens, tools):
    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.lifespan():
            yield

    app = FastAPI(lifespan=lifespan)
    app.mount("/mcp", mcp_app)
    token = tokens.issue(CTX, 600)
    async with serve(app) as base:
        async with session(f"{base}/mcp/", token) as s:
            result = await s.call_tool("echo", {"text": "mounted"})
            assert json.loads(result.content[0].text)["echo"] == "mounted"
        async with session(f"{base}/mcp/", token) as s:  # a second, independent session
            assert (await s.list_tools()).tools


async def test_rejected_without_valid_token(mcp_app, tokens):
    async with serve(mcp_app) as base:
        async with httpx.AsyncClient() as client:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            headers = {"Accept": "application/json, text/event-stream"}
            none = await client.post(f"{base}/", json=body, headers=headers)
            assert none.status_code == 401
            bad = await client.post(f"{base}/", json=body,
                                    headers={**headers, "Authorization": "Bearer nope"})
            assert bad.status_code == 401
            token = tokens.issue(CTX, 600)
            tokens.revoke(token)
            revoked = await client.post(f"{base}/", json=body,
                                        headers={**headers, "Authorization": f"Bearer {token}"})
            assert revoked.status_code == 401
        with pytest.raises(BaseException):
            async with session(f"{base}/", "nope"):
                pass
