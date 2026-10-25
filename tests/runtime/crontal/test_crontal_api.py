from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from handler.runtime.context import RunContext
from handler.runtime.gateway.crontal_api import CrontalApiTools, crontal_base_url

AGENT = "agent-secret"


class FakeCrontal:
    def __init__(self) -> None:
        self.minted = 0
        self.requests: list[httpx.Request] = []
        self.expire_next = False
        self.described = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        auth = request.headers.get("authorization", "")
        if path == "/api/v1/agent-access/token":
            assert auth == f"Bearer {AGENT}"
            self.minted += 1
            expires = datetime.now(timezone.utc) + timedelta(minutes=15)
            return httpx.Response(200, json={"token": f"jwt-{self.minted}",
                                             "expires_at": expires.isoformat()})
        if path == "/api/v1/agent-access/routes":
            return httpx.Response(200, json={"routes": [
                {"methods": ["GET", "POST"], "path": "/api/v1/rfq", "summary": "RFQs"},
                {"methods": ["GET"], "path": "/api/v1/files/merchant", "summary": "Files"},
                {"methods": ["POST"], "path": "/api/v1/files/upload",
                 "summary": "Upload a file document"},
                {"methods": ["GET"], "path": "/api/v1/auth/profile", "summary": "Me"},
            ]})
        if path == "/api/v1/agent-access/routes/describe":
            assert auth == f"Bearer {AGENT}"
            self.described += 1
            q = dict(request.url.params)
            if q["path"].startswith("/api/v1/rfq"):
                return httpx.Response(200, json={
                    "method": q["method"], "path": "/api/v1/rfq", "has_schema": True,
                    "body": {"type": "object", "required": ["name"]}})
            return httpx.Response(404, json={"error": "no route"})
        if not auth.startswith("Bearer jwt-"):
            return httpx.Response(401, json={"error": "unauthorized"})
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"error": "expired"})
        if path == "/api/v1/rfq" and request.method == "GET":
            return httpx.Response(200, json={"rfqs": [{"id": 1}], "q": dict(request.url.params)})
        if path == "/api/v1/files/7":
            return httpx.Response(200, content=b"name,qty\npipe,4\n",
                                  headers={"content-type": "text/csv",
                                           "content-disposition": 'attachment; filename="po.csv"'})
        if path == "/api/v1/files/upload":
            return httpx.Response(201, json={"ok": True, "body": request.content.decode(
                "latin-1")[:2000]})
        if path == "/api/v1/rfq" and request.method == "POST":
            return httpx.Response(400, json={"error": "name: Field required"})
        if path == "/api/v1/message/chat/3":
            return httpx.Response(400, json={"error": "message is required"})
        if path == "/api/v1/rfq/1" and request.method == "PATCH":
            return httpx.Response(200, json={"updated": json.loads(request.content)})
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def setup(tmp_path: Path):
    fake = FakeCrontal()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    tools = CrontalApiTools("https://crontal.test", AGENT,
                            workspace_for=lambda key: tmp_path / "ws" / key.replace(":", "_"),
                            client=client)
    by_name = {spec.name: spec for spec in tools.tools()}
    ctx = RunContext(run_id="r1", thread_key="chat:7:c1", user_id="7", acting_as="7")
    return fake, by_name, ctx, tmp_path / "ws" / "chat_7_c1"


def test_base_url_is_the_origin():
    assert crontal_base_url("https://x.herokuapp.com/api/v1/agent-mcp") == "https://x.herokuapp.com"


def test_specs_and_risks(setup):
    _, tools, _, _ = setup
    assert {n: str(s.risk) for n, s in tools.items()} == {
        "crontal_api_routes": "read", "crontal_api_describe": "read",
        "crontal_api_get": "read", "crontal_api_call": "write_external"}


async def test_get_uses_a_minted_user_token_and_caches_it(setup):
    fake, tools, ctx, _ = setup
    first = await tools["crontal_api_get"].handler(ctx, {"path": "/api/v1/rfq",
                                                         "query": {"status": "published"}})
    second = await tools["crontal_api_get"].handler(ctx, {"path": "/api/v1/rfq"})
    assert not first.is_error and first.content["body"]["rfqs"] == [{"id": 1}]
    assert first.content["body"]["q"] == {"status": "published"}
    assert second.content["status"] == 200 and fake.minted == 1
    mint = fake.requests[0]
    assert mint.headers["x-crontal-user"] == "7"


async def test_expired_token_is_refreshed_once(setup):
    fake, tools, ctx, _ = setup
    await tools["crontal_api_get"].handler(ctx, {"path": "/api/v1/rfq"})
    fake.expire_next = True
    result = await tools["crontal_api_get"].handler(ctx, {"path": "/api/v1/rfq"})
    assert result.content["status"] == 200 and fake.minted == 2


@pytest.mark.parametrize("path", [
    "/admin", "rfq", "/api/v1/../x", "/api/v1/agent-access/token",
    "/api/v1/handler/chat", "/api/v1/agent-mcp",
    # Site-admin tools, and ways to spell them that the server would resolve.
    "/api/v1/admin/users", "/api/v1/admin", "/api/v1/marketplace/query/RFQ",
    "/api/v1/files/stats", "/api/v1/files/stats?x=1", "/api/v1/ADMIN/users",
    "/api/v1/%61dmin/users", "/api/v1//admin/users", "/api/v1/./admin/users",
    "/api/v1/rfq/../admin", "/api/v1/rfq/..%2fadmin", "/api/v1\\admin",
])
async def test_paths_are_restricted(setup, path):
    fake, tools, ctx, _ = setup
    result = await tools["crontal_api_get"].handler(ctx, {"path": path})
    assert result.is_error and fake.requests == []
    called = await tools["crontal_api_call"].handler(ctx, {"method": "POST", "path": path})
    assert called.is_error and fake.requests == []
    described = await tools["crontal_api_describe"].handler(
        ctx, {"method": "GET", "path": path})
    assert described.is_error and fake.requests == []


async def test_hidden_routes_are_not_listed(tmp_path):
    fake = FakeCrontal()

    def with_admin(request: httpx.Request) -> httpx.Response:
        response = fake(request)
        if request.url.path == "/api/v1/agent-access/routes":
            return httpx.Response(200, json={"routes": response.json()["routes"] + [
                {"methods": ["GET"], "path": "/api/v1/admin/users", "summary": "Users"},
                {"methods": ["POST"], "path": "/api/v1/marketplace/query/<model_cls>"},
                {"methods": ["GET"], "path": "/api/v1/files/stats", "summary": "Stats"},
            ]})
        return response

    tools = CrontalApiTools("https://crontal.test", AGENT, workspace_for=lambda key: tmp_path,
                            client=httpx.AsyncClient(transport=httpx.MockTransport(with_admin)))
    routes = {s.name: s for s in tools.tools()}["crontal_api_routes"]
    ctx = RunContext(run_id="r1", thread_key="chat:7:c1", user_id="7", acting_as="7")
    listed = (await routes.handler(ctx, {})).content
    assert len(listed) == 4
    assert not any("admin" in r or "marketplace" in r or "stats" in r for r in listed)


async def test_files_are_saved_to_the_workspace_with_text(setup):
    _, tools, ctx, workspace = setup
    result = await tools["crontal_api_get"].handler(ctx, {"path": "/api/v1/files/7"})
    assert not result.is_error
    assert result.content["saved_to"] == "downloads/po.csv"
    assert (workspace / "downloads" / "po.csv").read_bytes() == b"name,qty\npipe,4\n"
    assert "pipe" in result.content["text"]


async def test_call_sends_json_and_uploads_files(setup):
    _, tools, ctx, workspace = setup
    patched = await tools["crontal_api_call"].handler(
        ctx, {"method": "PATCH", "path": "/api/v1/rfq/1", "json": {"name": "New"}})
    assert patched.content["body"] == {"updated": {"name": "New"}}

    (workspace / "attachments").mkdir(parents=True)
    (workspace / "attachments" / "spec.txt").write_text("316L")
    uploaded = await tools["crontal_api_call"].handler(
        ctx, {"method": "POST", "path": "/api/v1/files/upload",
              "upload_file": "attachments/spec.txt", "form": {"entity_type": "rfq"}})
    body = uploaded.content["body"]["body"]
    assert uploaded.content["status"] == 201
    assert 'filename="spec.txt"' in body and "316L" in body and "entity_type" in body

    escape = await tools["crontal_api_call"].handler(
        ctx, {"method": "POST", "path": "/api/v1/files/upload", "upload_file": "../../etc/passwd"})
    assert escape.is_error
    bad = await tools["crontal_api_call"].handler(ctx, {"method": "GET", "path": "/api/v1/rfq"})
    assert bad.is_error


async def test_http_errors_are_tool_errors(setup):
    _, tools, ctx, _ = setup
    result = await tools["crontal_api_get"].handler(ctx, {"path": "/api/v1/nope"})
    assert result.is_error and result.content["status"] == 404


async def test_routes_are_listed_and_searchable(setup):
    _, tools, ctx, _ = setup
    everything = await tools["crontal_api_routes"].handler(ctx, {})
    assert len(everything.content) == 4
    assert everything.content[0] == "GET,POST /api/v1/rfq — RFQs"
    assert (await tools["crontal_api_routes"].handler(ctx, {"search": "  "})).content \
        == everything.content
    files = await tools["crontal_api_routes"].handler(ctx, {"search": "file"})
    assert files.content == ["GET /api/v1/files/merchant — Files",
                             "POST /api/v1/files/upload — Upload a file document"]
    # Any word matches; routes matching more words rank first.
    ranked = await tools["crontal_api_routes"].handler(
        ctx, {"search": "files list uploaded documents"})
    assert ranked.content[0].startswith("POST /api/v1/files/upload")
    assert len(ranked.content) == 2
    assert (await tools["crontal_api_routes"].handler(ctx, {"search": "spreadsheet"})
            ).content == []


async def test_describe_returns_the_spec_and_caches_it(setup):
    fake, tools, ctx, _ = setup
    first = await tools["crontal_api_describe"].handler(
        ctx, {"method": "post", "path": "/api/v1/rfq"})
    again = await tools["crontal_api_describe"].handler(
        ctx, {"method": "POST", "path": "/api/v1/rfq"})
    assert not first.is_error and first.content["body"]["required"] == ["name"]
    assert again.content == first.content and fake.described == 1
    missing = await tools["crontal_api_describe"].handler(
        ctx, {"method": "GET", "path": "/api/v1/nope"})
    assert missing.is_error and "crontal_api_routes" in missing.content
    for bad in ({"method": "HEAD", "path": "/api/v1/rfq"},
                {"method": "GET", "path": "/api/v1/handler/x"}):
        assert (await tools["crontal_api_describe"].handler(ctx, bad)).is_error
    assert fake.described == 2


async def test_validation_errors_carry_the_spec(setup):
    fake, tools, ctx, _ = setup
    result = await tools["crontal_api_call"].handler(
        ctx, {"method": "POST", "path": "/api/v1/rfq", "json": {"title": "x"}})
    assert result.is_error and result.content["status"] == 400
    assert result.content["expected"]["body"]["required"] == ["name"]
    assert "expected" in result.content["hint"]
    # No spec available: the plain error is kept.
    other = await tools["crontal_api_call"].handler(
        ctx, {"method": "POST", "path": "/api/v1/message/chat/3", "json": {"content": "x"}})
    assert other.is_error and "expected" not in other.content
    # Other errors do not fetch a spec.
    before = fake.described
    await tools["crontal_api_get"].handler(ctx, {"path": "/api/v1/nope"})
    assert fake.described == before


async def test_upload_field_names_the_multipart_part(setup):
    _, tools, ctx, workspace = setup
    (workspace / "a").mkdir(parents=True)
    (workspace / "a" / "cert.pdf").write_bytes(b"%PDF")
    result = await tools["crontal_api_call"].handler(
        ctx, {"method": "POST", "path": "/api/v1/files/upload", "upload_file": "a/cert.pdf",
              "upload_field": "document"})
    assert 'name="document"; filename="cert.pdf"' in result.content["body"]["body"]


async def test_instance_runs_without_a_user_are_refused(setup):
    fake, tools, _, _ = setup
    ctx = RunContext(run_id="r", thread_key="automation:x", user_id=None, acting_as=None)
    result = await tools["crontal_api_get"].handler(ctx, {"path": "/api/v1/rfq"})
    assert result.is_error and fake.requests == []
