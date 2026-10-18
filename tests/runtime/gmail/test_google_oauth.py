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
    user_owner,
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
    assert (await client.post("/api/connections/google/start")).status_code == 401
    status = (await client.get("/api/connections/google", headers=member)).json()
    assert status["configured"] and status["connection"] is None
    assert status["redirect_uri"] == REDIRECT
    start = (await client.post("/api/connections/google/start", headers=member)).json()
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
    assert (await world.oauth.connection(user_owner("alice"))).id == conn["id"]
    assert (await client.get("/api/connections/google", headers=admin)).json()[
        "connection"] is None
    url = f"/api/connections/google/{conn['id']}"
    assert (await client.delete(url, headers=admin)).status_code == 404
    assert (await client.delete(url, headers=member)).json() == {"deleted": True,
                                                                 "revoked": True}
    assert (await client.delete(url, headers=member)).status_code == 404


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


# ---- return_to (Crontal's settings page) -------------------------------------------

from handler.runtime.web.google_api import (  # noqa: E402
    allowed_return_to,
    parse_origins,
)

CRONTAL = "https://crontal.test"
RETURN_TO = "https://crontal.test/app/settings?tab=handler"


def test_parse_origins_normalises_and_ignores_non_origins():
    origins = parse_origins(" https://Crontal.test/ ,http://localhost:5173,"
                            "https://x.test/path, ftp://f.test, https://y.test:443,,")
    assert origins == {"https://crontal.test", "http://localhost:5173",
                       "https://y.test"}
    assert parse_origins(None) == frozenset() == parse_origins("")


@pytest.mark.parametrize("url, ok", [
    (RETURN_TO, True),
    ("https://crontal.test", True),
    ("https://CRONTAL.test:443/x#frag", True),
    ("http://crontal.test/x", False),           # scheme is part of the origin
    ("https://crontal.test:8443/x", False),
    ("https://evil.test/?u=https://crontal.test", False),
    ("https://crontal.test.evil.test/", False),
    ("https://crontal.test@evil.test/", False),  # userinfo
    ("https://evil.test\\@crontal.test/", False),
    ("//evil.test/", False),
    ("/app/settings", False),
    ("javascript:alert(1)", False),
    ("https://crontal.test/\nx", False),
    ("", False),
    (None, False),
])
def test_allowed_return_to(url, ok):
    assert allowed_return_to(url, parse_origins(CRONTAL)) is ok


@pytest.fixture
async def web_rt(world):
    app = FastAPI()
    app.include_router(google_router(world.oauth, Authenticator(world.store, ADMIN_KEY),
                                     return_to_origins=CRONTAL))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://handler.test") as client:
        yield client, {"Authorization": f"Bearer {ADMIN_KEY}"}


async def _start(client, admin, return_to):
    return await client.post("/api/connections/google/start", headers=admin,
                             json={"return_to": return_to})


async def test_return_to_success_redirects_back(web_rt):
    client, admin = web_rt
    response = await _start(client, admin, RETURN_TO + "&google=old&reason=stale")
    assert response.status_code == 200
    state = _query(response.json()["url"])["state"]
    done = await client.get("/oauth/google/callback",
                            params={"code": "good-code", "state": state})
    assert done.status_code == 303
    location = done.headers["location"]
    assert location.startswith("https://crontal.test/app/settings?")
    assert _query(location) == {"tab": "handler", "google": "connected"}


async def test_return_to_error_redirects_back_with_reason(web_rt):
    client, admin = web_rt
    state = _query((await _start(client, admin, RETURN_TO)).json()["url"])["state"]
    denied = await client.get("/oauth/google/callback",
                              params={"error": "access_denied", "state": state})
    query = _query(denied.headers["location"])
    assert denied.headers["location"].startswith("https://crontal.test/app/settings?")
    assert query["google"] == "error" and "access_denied" in query["reason"]
    # A failed code exchange also goes back with a reason.
    state = _query((await _start(client, admin, RETURN_TO)).json()["url"])["state"]
    bad = await client.get("/oauth/google/callback",
                           params={"code": "bad-code", "state": state})
    assert _query(bad.headers["location"])["google"] == "error"
    assert bad.headers["location"].startswith(CRONTAL)


async def test_return_to_outside_allowlist_is_rejected(web_rt):
    client, admin = web_rt
    for url in ("https://evil.test/", "https://crontal.test@evil.test/",
                "/relative", "javascript:alert(1)"):
        response = await _start(client, admin, url)
        assert response.status_code == 400, url
        assert "HANDLER_RETURN_TO_ORIGINS" in response.json()["detail"]


async def test_return_to_rejected_when_allowlist_empty(web):
    client, admin, _ = web
    assert (await _start(client, admin, RETURN_TO)).status_code == 400


async def test_forged_or_unsigned_return_to_falls_back_home(web_rt, world):
    client, admin = web_rt
    # A state signed by someone else (wrong key) carrying an evil return_to.
    forged = SecretBox("another-key-entirely-0123456789").sign(
        {"n": "x", "by": "root", "ru": REDIRECT, "o": "instance",
         "rt": "https://evil.test/"}, 600)
    response = await client.get("/oauth/google/callback",
                                params={"code": "good-code", "state": forged})
    assert response.headers["location"].startswith("/app?google_error=")


async def test_signed_return_to_rechecked_against_allowlist(world):
    # Signed with the real key, but the origin isn't allowed (allowlist shrank).
    state = _query(world.oauth.start(started_by="root", redirect_uri=REDIRECT,
                                     return_to="https://old.test/x"))["state"]
    app = FastAPI()
    app.include_router(google_router(world.oauth, Authenticator(world.store, ADMIN_KEY),
                                     return_to_origins=CRONTAL))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://handler.test") as client:
        done = await client.get("/oauth/google/callback",
                                params={"code": "good-code", "state": state})
    assert done.headers["location"].startswith("/app?google=connected")


async def test_return_to_origins_read_from_env(world, monkeypatch):
    monkeypatch.setenv("HANDLER_RETURN_TO_ORIGINS", "https://crontal.test")
    app = FastAPI()
    app.include_router(google_router(world.oauth, Authenticator(world.store, ADMIN_KEY)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://handler.test") as client:
        admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
        assert (await _start(client, admin, RETURN_TO)).status_code == 200
        assert (await _start(client, admin, "https://evil.test/")).status_code == 400
