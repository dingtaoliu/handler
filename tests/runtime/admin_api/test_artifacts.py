import httpx
from fastapi import FastAPI

from handler.runtime.artifacts import ArtifactStore
from handler.runtime.auth import Authenticator
from handler.runtime.context import RunContext
from handler.runtime.files import LocalFileStore
from handler.runtime.gateway import Gateway, GrantPolicy
from handler.runtime.tools.spreadsheets import SpreadsheetTools
from handler.runtime.web.admin_api import admin_router


async def test_artifact_download_uses_existing_auth_and_survives_reload(world):
    artifacts = ArtifactStore(LocalFileStore(world.tmp_path / "durable"))
    tool = SpreadsheetTools(lambda _: world.tmp_path / "workspace", artifacts).tools()[0]
    result = await tool.handler(RunContext("r", "chat:alice:c", "alice", "alice"), {
        "path": "报价.csv", "columns": [{"key": "id", "heading": "ID", "type": "text"}],
        "rows": [{"id": "01178"}]})
    file_id = result.content["artifact"]["id"]
    # A new app with an empty local upload cache still fetches the durable artifact.
    app = FastAPI()
    auth = Authenticator(world.store)
    async def emit(event):
        pass
    gateway = Gateway(world.store, GrantPolicy({}), emit_event=emit)
    app.include_router(admin_router(world.store, auth, scheduler=world.scheduler, gateway=gateway,
        uploads=world.uploads, artifact_store=ArtifactStore(LocalFileStore(world.tmp_path / "durable"))))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/uploads/{file_id}"
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers=world.h("bob"))).status_code == 404
        for name in ["alice", "root"]:
            response = await client.get(path, headers=world.h(name))
            assert response.status_code == 200
            assert response.headers["X-Handler-Upload-Owner"] == "alice"
            assert "filename*=UTF-8''" in response.headers["content-disposition"]
            assert response.content.decode("utf-8-sig") == "ID\r\n01178\r\n"
