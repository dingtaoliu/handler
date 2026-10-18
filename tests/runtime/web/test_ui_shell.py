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
    assert 'location.replace("/")' in login.text
    assert (await client.get("/timeline")).status_code == 401


async def test_data_apis_still_require_auth(client):
    assert (await client.get("/api/me")).status_code == 401
