"""Crontal integration (handler side): webhook ingress and the Crontal connector."""

from __future__ import annotations

import json

import mcp.types as mt
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import request_ctx
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.responses import JSONResponse

from handler.runtime.context import RunContext
from handler.runtime.gateway.crontal import CRONTAL_TOOLS, CrontalCredentials, crontal_connector
from handler.runtime.gateway.gateway import Gateway
from handler.runtime.gateway.policy import GrantPolicy
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import ApprovalStatus, ThreadType
from handler.runtime.web.ingest import crontal_ingest_router, sign, verify
from tests.runtime.upstream.conftest import serve

SECRET = "whsec-test"


def _body(note_id=1, entity=("rfq", 981), recipient=17) -> bytes:
    return json.dumps({"id": note_id, "type": "rfq_published", "recipient_user_id": recipient,
                       "entity_type": entity[0] if entity else None,
                       "entity_id": entity[1] if entity else None,
                       "data": {"title": "316L flat bar"}, "created_at": "2026-10-17T00:00:00Z"}
                      ).encode()


def test_signature_verify():
    body = b'{"x":1}'
    assert verify(SECRET, sign(SECRET, body), body)
    assert not verify(SECRET, sign("other", body), body)
    assert not verify(SECRET, sign(SECRET, body), body + b" ")
    assert not verify(SECRET, sign(SECRET, body, timestamp=1000), body, now=1000 + 301)
    assert not verify(SECRET, "garbage", body) and not verify(SECRET, None, body)


async def test_ingest_routes_notifications_to_entity_threads(tmp_path):
    store = SQLiteStore(tmp_path / "h.db")
    app = FastAPI()
    app.include_router(crontal_ingest_router(store, SECRET))
    client = TestClient(app)

    def post(body, sig=None):
        return client.post("/ingest/crontal", content=body,
                           headers={"x-crontal-signature": sig or sign(SECRET, body)})

    first = post(_body(1))
    assert first.status_code == 202 and first.json()["thread_key"] == "crontal:rfq:981"
    assert post(_body(1)).status_code == 200  # duplicate delivery
    assert post(_body(2, recipient=18)).status_code == 202  # same entity, another recipient
    assert post(_body(3, entity=None)).json()["thread_key"] == "crontal:notification:3"
    assert post(_body(4), sig="t=1,v1=bad").status_code == 401
    assert client.post("/ingest/crontal", content=b"{}",
                       headers={"x-crontal-signature": sign(SECRET, b"{}")}).status_code == 422

    thread = await store.get_thread("crontal:rfq:981")
    assert thread.type is ThreadType.CRONTAL and thread.owner_user_id == "17"
    events = await store.pending_events("crontal:rfq:981")
    assert [e.idempotency_key for e in events] == ["crontal:1", "crontal:2"]  # coalesced
    assert events[0].type == "crontal.notification" and events[0].payload["data"]["title"]
    await store.close()


async def test_credentials_act_as_the_user():
    creds = CrontalCredentials("https://crontal.test/api/v1/agent-mcp", "agent-tok")
    access = await creds.resolve("crontal", "user:17")
    assert access.headers == {"Authorization": "Bearer agent-tok", "X-Crontal-User": "17"}
    assert await creds.resolve("crontal", "instance") is None
    assert "X-Crontal-User" not in (await creds.discovery_access("crontal")).headers


class FakeCrontal:
    """Crontal's MCP endpoint: token for discovery, X-Crontal-User for calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []
        self.manager = StreamableHTTPSessionManager(self._server(), json_response=True,
                                                    stateless=True)

    def _server(self) -> Server:
        server: Server = Server("fake-crontal")
        schema = {"type": "object", "properties": {"rfq_id": {"type": "integer"}}}

        @server.list_tools()
        async def list_tools():
            return [mt.Tool(name=n, description=f"Crontal {n}", inputSchema=schema)
                    for n in CRONTAL_TOOLS] + [mt.Tool(name="file_write", description="x",
                                                       inputSchema=schema)]

        @server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            user = request_ctx.get().request.headers.get("x-crontal-user")
            self.calls.append((name, arguments, user))
            return mt.CallToolResult(content=[mt.TextContent(
                type="text", text=json.dumps({"user": user, "tool": name}))])

        return server

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await receive()
            async with self.manager.run():
                await send({"type": "lifespan.startup.complete"})
                await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        if headers.get("authorization") != "Bearer agent-tok":
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)


async def test_connector_curates_tools_and_acts_as_user(tmp_path):
    fake = FakeCrontal()
    async with serve(fake) as url:
        connector = crontal_connector(f"{url}/mcp", "agent-tok")
        await connector.start()
        names = {s.name for s in connector.tools()}
        assert "crontal_get_rfqs" in names and "crontal_create_rfq" in names
        assert not any("file_write" in n for n in names)  # not curated

        store = SQLiteStore(tmp_path / "h.db")
        events = []

        async def emit(event):
            events.append(event)

        gateway = Gateway(store, GrantPolicy(), emit_event=emit)
        gateway.register(connector)
        ctx = RunContext(run_id="r1", thread_key="crontal:rfq:981", user_id="17", acting_as="17")
        read = await gateway.call(ctx, "crontal_get_rfq", {"rfq_id": 981})
        assert not read.is_error and fake.calls[-1] == ("app_get_rfq", {"rfq_id": 981}, "17")

        write = await gateway.call(ctx, "crontal_publish_rfq", {"rfq_id": 981})
        assert write.content["status"] == "pending_approval"  # write_external → ask
        approval = await gateway.approve(write.content["approval_id"], decided_by="17")
        assert approval.status is ApprovalStatus.EXECUTED
        assert fake.calls[-1] == ("app_publish_rfq", {"rfq_id": 981}, "17")

        agentless = RunContext(run_id="r2", thread_key="automation:x")
        assert (await gateway.call(agentless, "crontal_get_rfqs", {})).is_error
        await store.close()
