from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI

from handler.runtime.auth import Authenticator
from handler.runtime.gateway.credentials import NeedsReauth
from handler.runtime.gateway.google_oauth import (
    DEFAULT_SCOPES,
    GMAIL_MODIFY,
    GoogleOAuth,
    GoogleOAuthConfig,
    OAuthError,
    google_oauth_from_env,
)
from handler.runtime.gateway.secret_box import SecretBox
from handler.runtime.store.sqlite_users import User
from handler.runtime.web.google_api import google_router

from .conftest import ACCOUNT, REDIRECT


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(httpx.URL(url).query.decode()).items()}


def test_default_scopes_are_email_focused():
    assert DEFAULT_SCOPES == ("openid", "https://www.googleapis.com/auth/userinfo.email",
                              "https://www.googleapis.com/auth/gmail.modify")


def test_config_from_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    assert GoogleOAuthConfig.from_env() is None
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("HANDLER_PUBLIC_URL", "https://agent.test/")
    monkeypatch.delenv("GOOGLE_OAUTH_SCOPES", raising=False)
    config = GoogleOAuthConfig.from_env()
    assert (config.public_url, config.scopes) == ("https://agent.test", DEFAULT_SCOPES)
    monkeypatch.setenv("GOOGLE_OAUTH_SCOPES", "openid  https://mail.google.com/")
    assert GoogleOAuthConfig.from_env().scopes == ("openid", "https://mail.google.com/")


def test_from_env_needs_a_secret_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.delenv("HANDLER_SECRET_KEY", raising=False)
    monkeypatch.delenv("HANDLER_ADMIN_KEY", raising=False)
    assert google_oauth_from_env(object()) is None
    monkeypatch.setenv("HANDLER_ADMIN_KEY", "admin")
    assert isinstance(google_oauth_from_env(object()), GoogleOAuth)


async def test_start_builds_the_consent_url(world):
    query = _query(world.oauth.start(started_by="root", redirect_uri=REDIRECT))
    assert query["client_id"] == "cid" and query["redirect_uri"] == REDIRECT
    assert query["scope"] == " ".join(DEFAULT_SCOPES)
    assert (query["access_type"], query["prompt"], query["response_type"]) == (
        "offline", "consent", "code")
    assert query["state"]
    assert world.oauth.redirect_uri("http://x") == "http://x/oauth/google/callback"
    public = GoogleOAuth(GoogleOAuthConfig("c", "s", "https://pub.test"), world.store,
                         SecretBox("k"))
    assert public.redirect_uri("http://internal") == "https://pub.test/oauth/google/callback"


async def test_complete_stores_an_encrypted_refresh_token(world):
    connection = await world.connect()
    assert (connection.connector, connection.owner, connection.provider) == (
        "google", "instance", "local")
    assert connection.external_ref == ACCOUNT and connection.created_by == "root"
    assert GMAIL_MODIFY in connection.scopes
    stored = await world.store.get_secret(connection.secret_ref)
    assert stored and "rt-1" not in stored
    assert SecretBox("k" * 32).decrypt(stored) == "rt-1"
    access = await world.oauth.resolve("google", "instance")
    assert access.headers == {"Authorization": "Bearer at-1"}  # cached, no refresh
    assert world.google.issued == 1
    assert await world.oauth.resolve("other", "instance") is None


async def test_state_is_signed_expiring_and_single_use(world):
    url = world.oauth.start(started_by="root", redirect_uri=REDIRECT)
    state = _query(url)["state"]
    with pytest.raises(OAuthError, match="invalid or expired"):
        await world.oauth.complete("good-code", state[:-2] + "xx")
    other = SecretBox("another key").sign({"n": "1", "by": "x", "ru": REDIRECT,
                                           "o": "instance"}, 600)
    with pytest.raises(OAuthError, match="invalid or expired"):
        await world.oauth.complete("good-code", other)
    await world.oauth.complete("good-code", state)
    with pytest.raises(OAuthError, match="already used"):
        await world.oauth.complete("good-code", state)
    late = _query(world.oauth.start(started_by="root", redirect_uri=REDIRECT))["state"]
    world.clock.now += 601
    with pytest.raises(OAuthError, match="invalid or expired"):
        await world.oauth.complete("good-code", late)


async def test_failed_exchange_and_missing_refresh_token(world):
    url = world.oauth.start(started_by="root", redirect_uri=REDIRECT)
    with pytest.raises(OAuthError, match="code exchange"):
        await world.oauth.complete("bad-code", _query(url)["state"])
    world.google.return_refresh = False
    url = world.oauth.start(started_by="root", redirect_uri=REDIRECT)
    with pytest.raises(OAuthError, match="no refresh token"):
        await world.oauth.complete("good-code", _query(url)["state"])
    assert await world.store.list_connections() == []


async def test_access_token_refreshes_near_expiry(world):
    await world.connect()
    world.clock.now += 3599 - 60  # inside the refresh margin
    access = await world.oauth.resolve("google", "instance")
    assert access.headers["Authorization"] == "Bearer at-2"
    refreshes = [r for r in world.google.requests
                 if r.url.path == "/token" and b"refresh_token" in r.content]
    assert len(refreshes) == 1
    assert (await world.oauth.resolve("google", "instance")).headers == access.headers


async def test_invalid_grant_marks_the_connection_for_reconnect(world):
    connection = await world.connect()
    world.oauth.invalidate()
    world.google.refresh_mode = "invalid_grant"
    with pytest.raises(NeedsReauth):
        await world.oauth.resolve("google", "instance")
    stored = await world.store.get_connection(connection.id)
    assert stored.status == "needs_reauth"
    calls = len(world.google.requests)
    with pytest.raises(NeedsReauth):  # known state: no further call to Google
        await world.oauth.resolve("google", "instance")
    assert len(world.google.requests) == calls
    assert (await world.oauth.connection()).status == "needs_reauth"


async def test_invalid_client_is_an_error_not_a_reconnect(world):
    connection = await world.connect()
    world.oauth.invalidate()
    world.google.refresh_mode = "invalid_client"
    with pytest.raises(OAuthError, match="GOOGLE_OAUTH_CLIENT_ID"):
        await world.oauth.resolve("google", "instance")
    assert (await world.store.get_connection(connection.id)).status == "active"


async def test_unreadable_secret_needs_reconnect(world):
    connection = await world.connect()
    other = GoogleOAuth(world.oauth.config, world.store, SecretBox("rotated key"),
                        client=world.oauth._client)
    with pytest.raises(NeedsReauth):
        await other.resolve("google", "instance")
    assert (await world.store.get_connection(connection.id)).status == "needs_reauth"


async def test_reconnect_replaces_without_revoking(world):
    first = await world.connect()
    second = await world.connect()
    assert [c.id for c in await world.store.list_connections()] == [second.id]
    assert await world.store.get_secret(first.secret_ref) is None
    assert world.google.revoked == []


async def test_disconnect_revokes_and_deletes(world):
    connection = await world.connect()
    assert await world.oauth.disconnect(connection)
    assert world.google.revoked == ["rt-1"]
    assert await world.store.list_connections() == []
    assert await world.store.get_secret(connection.secret_ref) is None
    assert await world.oauth.resolve("google", "instance") is None


# ---- HTTP routes ------------------------------------------------------------------

ADMIN_KEY = "admin-key-0123456789"


@pytest.fixture
async def web(world):
    await world.store.create_user(User("alice", "Alice", "member"))
    _, token = await world.store.create_token("alice", "t")
    app = FastAPI()
    app.include_router(google_router(world.oauth, Authenticator(world.store, ADMIN_KEY)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://handler.test") as client:
        yield client, {"Authorization": f"Bearer {ADMIN_KEY}"}, {
            "Authorization": f"Bearer {token}"}


async def test_routes_connect_status_disconnect(web, world):
    client, admin, member = web
    assert (await client.post("/api/connections/google/start", headers=member)).status_code == 403
    assert (await client.post("/api/connections/google/start")).status_code == 401
    status = (await client.get("/api/connections/google", headers=member)).json()
    assert status["configured"] and status["connection"] is None
    assert status["redirect_uri"] == REDIRECT
    start = (await client.post("/api/connections/google/start", headers=admin)).json()
    assert start["redirect_uri"] == REDIRECT
    state = _query(start["url"])["state"]

    bad = await client.get("/oauth/google/callback", params={"code": "good-code",
                                                              "state": "forged.sig"})
    assert bad.status_code == 303 and "google_error=" in bad.headers["location"]
    denied = await client.get("/oauth/google/callback", params={"error": "access_denied"})
    assert "access_denied" in denied.headers["location"]
    done = await client.get("/oauth/google/callback",
                            params={"code": "good-code", "state": state})
    assert done.status_code == 303
    assert done.headers["location"].startswith("/app?google=connected")
    assert done.headers["location"].endswith("#connections")

    status = (await client.get("/api/connections/google", headers=member)).json()
    conn = status["connection"]
    assert conn["account"] == ACCOUNT and conn["status"] == "active"
    assert GMAIL_MODIFY in conn["scopes"]
    url = f"/api/connections/google/{conn['id']}"
    assert (await client.delete(url, headers=member)).status_code == 403
    assert (await client.delete(url, headers=admin)).json() == {"deleted": True,
                                                                "revoked": True}
    assert (await client.delete(url, headers=admin)).status_code == 404


async def test_forwarded_proto_builds_the_redirect_uri(web):
    client, admin, _ = web
    response = await client.post(
        "/api/connections/google/start", headers={**admin, "x-forwarded-proto": "https",
                                                  "host": "agent.herokuapp.com"})
    assert response.json()["redirect_uri"] == (
        "https://agent.herokuapp.com/oauth/google/callback")


async def test_routes_when_not_configured(world):
    app = FastAPI()
    app.include_router(google_router(None, Authenticator(world.store, ADMIN_KEY)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
        assert (await client.get("/api/connections/google", headers=admin)).json() == {
            "configured": False, "connection": None}
        assert (await client.post("/api/connections/google/start",
                                  headers=admin)).status_code == 503
        callback = await client.get("/oauth/google/callback")
        assert "google_error=" in callback.headers["location"]
