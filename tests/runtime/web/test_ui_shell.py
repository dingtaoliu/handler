from __future__ import annotations

import httpx
import pytest

from handler.runtime.auth import Authenticator
from handler.runtime.bus import RunEventBus
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.web.app import create_app


@pytest.fixture
async def client(tmp_path):
    users = SQLiteStore(tmp_path / "users.db")
    app = create_app(object(), RunEventBus(), Authenticator(users, "admin-key-for-tests"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        yield c
    await users.close()


@pytest.mark.parametrize("path", ["/", "/app", "/app/"])
async def test_shell_is_served_without_auth(client, path):
    r = await client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "/app/static/app.js" in r.text and "/app/static/style.css" in r.text
    assert r.headers["cache-control"] == "no-store"


async def test_shell_has_workspace_navigation_and_accessible_chat_controls(client):
    page = (await client.get("/app")).text
    assert 'placeholder="Search chats"' in page
    assert 'aria-label="Open navigation"' in page
    assert 'aria-label="Show conversations"' in page
    assert 'aria-label="Send message"' in page
    assert "Agent workspace" in page


@pytest.mark.parametrize("name,kind", [("app.js", "javascript"), ("style.css", "css"),
                                       ("logo.svg", "svg")])
async def test_assets_are_served(client, name, kind):
    r = await client.get(f"/app/static/{name}")
    assert r.status_code == 200 and kind in r.headers["content-type"]


@pytest.mark.parametrize("name", ["missing.js", "..%2Flogin.html", "index.html"])
async def test_asset_lookup_is_confined(client, name):
    assert (await client.get(f"/app/static/{name}")).status_code == 404


async def test_login_redirects_to_shell_and_timeline_stays_protected(client):
    login = await client.get("/login")
    assert 'location.replace(handlerPath("/"))' in login.text
    assert (await client.get("/timeline")).status_code == 401


async def test_data_apis_still_require_auth(client):
    assert (await client.get("/api/me")).status_code == 401


async def test_ui_can_boot_without_a_stored_token_in_trusted_mode(client):
    script = (await client.get("/app/static/app.js")).text
    assert 'if (token) h.Authorization = "Bearer " + token' in script
    assert 'if (!store.get("handler.token")) { goLogin(); return; }' not in script
    login = (await client.get("/login")).text
    assert 'enter(token || ""' in login
