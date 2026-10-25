"""One-click sign-in: tickets minted with the admin key, exchanged once."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import httpx
import pytest

from handler.runtime.auth import Authenticator
from handler.runtime.bus import RunEventBus
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.store.sqlite_users import User
from handler.runtime.web.app import create_app
from handler.runtime.web.auth_api import (
    SESSION_TTL_SECONDS,
    TICKET_TTL_SECONDS,
    ticket_hash,
)
from tests.runtime.auth.conftest import ADMIN_KEY

ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
SITE_ADMIN = {"user_id": "42", "display_name": "Ada Admin", "role": "admin"}


# --- store --------------------------------------------------------------------
async def test_ticket_is_single_use_and_expires(users_store, clock):
    store = users_store
    await store.create_user(User("alice", "Alice"))
    expires = await store.create_sign_in_ticket("alice", "h1", 60)
    assert expires == clock() + timedelta(seconds=60)
    assert await store.redeem_sign_in_ticket("unknown") is None
    assert await store.redeem_sign_in_ticket("h1") == "alice"
    assert await store.redeem_sign_in_ticket("h1") is None  # single use

    await store.create_sign_in_ticket("alice", "h2", 60)
    clock.advance(60)
    assert await store.redeem_sign_in_ticket("h2") is None  # expired at exactly ttl


async def test_expired_tickets_are_pruned(users_store, clock):
    store = users_store
    await store.create_user(User("alice", "Alice"))
    await store.create_sign_in_ticket("alice", "old", 60)
    clock.advance(61)
    await store.create_sign_in_ticket("alice", "new", 60)
    clock.advance(-120)  # back before "old" expired: it was deleted, not just stale
    assert await store.redeem_sign_in_ticket("old") is None


async def test_token_ttl(users_store, clock):
    store = users_store
    await store.create_user(User("alice", "Alice"))
    _, forever = await store.create_token("alice", "cli")
    _, session = await store.create_token("alice", "session", ttl_seconds=30)
    assert (await store.resolve_token(session)).id == "alice"
    clock.advance(30)
    assert await store.resolve_token(session) is None
    assert (await store.resolve_token(forever)).id == "alice"


# --- HTTP ---------------------------------------------------------------------
@pytest.fixture
async def http(tmp_path, clock):
    store = SQLiteStore(tmp_path / "sso.db", clock=clock)
    app = create_app(store, RunEventBus(), Authenticator(store, ADMIN_KEY), instance_name="bss")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        client.store = store
        yield client
    await store.close()


async def _mint(http, body=None, headers=ADMIN):
    return await http.post("/api/auth/sign-in-tickets", json=body or SITE_ADMIN,
                           headers=headers)


async def test_full_flow_creates_admin_user_and_session(http):
    minted = await _mint(http)
    assert minted.status_code == 201, minted.text
    body = minted.json()
    assert body["expires_in"] == TICKET_TTL_SECONDS == 60
    assert body["sign_in_path"] == "/login" and body["role"] == "admin"
    assert minted.headers["cache-control"] == "no-store"
    user = await http.store.get_user("42")
    assert (user.display_name, user.role) == ("Ada Admin", "admin")

    exchanged = await http.post("/login/ticket", json={"ticket": body["ticket"]})
    assert exchanged.status_code == 200, exchanged.text
    session = exchanged.json()
    assert session["user_id"] == "42" and session["role"] == "admin"
    assert session["expires_in"] == SESSION_TTL_SECONDS
    assert exchanged.headers["cache-control"] == "no-store"
    me = await http.get("/api/me", headers={"Authorization": f"Bearer {session['token']}"})
    assert me.json() == {"user_id": "42", "role": "admin", "via": "user_token",
                         "display_name": "Ada Admin"}
    # A Crontal-made session can use admin APIs
    users = await http.get("/api/users", headers={"Authorization": f"Bearer {session['token']}"})
    assert users.status_code == 200

    again = await http.post("/login/ticket", json={"ticket": body["ticket"]})
    assert again.status_code == 401


async def test_ticket_hash_is_stored_not_ticket(http):
    ticket = (await _mint(http)).json()["ticket"]
    rows = await http.store._call(lambda db: [dict(r) for r in db.execute(
        "SELECT * FROM sign_in_tickets")])
    assert len(rows) == 1
    assert ticket not in str(rows)
    assert rows[0]["ticket_hash"] == ticket_hash("bss", ticket)
    assert rows[0]["ticket_hash"] != hashlib.sha256(ticket.encode()).hexdigest()


async def test_ticket_expires_after_ttl(http, clock):
    ticket = (await _mint(http)).json()["ticket"]
    clock.advance(TICKET_TTL_SECONDS + 1)
    assert (await http.post("/login/ticket", json={"ticket": ticket})).status_code == 401


async def test_session_expires(http, clock):
    ticket = (await _mint(http)).json()["ticket"]
    token = (await http.post("/login/ticket", json={"ticket": ticket})).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert (await http.get("/api/me", headers=auth)).status_code == 200
    clock.advance(SESSION_TTL_SECONDS)
    assert (await http.get("/api/me", headers=auth)).status_code == 401


async def test_ticket_from_another_instance_is_rejected(http):
    # Same database, different instance name: the hash binding differs.
    other = create_app(http.store, RunEventBus(), Authenticator(http.store, ADMIN_KEY),
                       instance_name="main")
    ticket = (await _mint(http)).json()["ticket"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other),
                                 base_url="http://t") as client:
        assert (await client.post("/login/ticket", json={"ticket": ticket})).status_code == 401
    assert (await http.post("/login/ticket", json={"ticket": ticket})).status_code == 200


async def test_role_and_name_follow_crontal(http):
    await http.store.create_user(User("42", "Old", "member"))
    await _mint(http)
    user = await http.store.get_user("42")
    assert (user.display_name, user.role) == ("Ada Admin", "admin")
    await _mint(http, {**SITE_ADMIN, "role": "member"})
    assert (await http.store.get_user("42")).role == "member"


async def test_disabled_user_cannot_get_a_ticket(http):
    await http.store.create_user(User("42", "Ada", "admin", disabled=True))
    assert (await _mint(http)).status_code == 403
    assert (await http.store.get_user("42")).disabled is True


async def test_user_disabled_after_mint_cannot_exchange(http):
    ticket = (await _mint(http)).json()["ticket"]
    await http.store.update_user("42", disabled=True)
    assert (await http.post("/login/ticket", json={"ticket": ticket})).status_code == 401


async def test_only_the_bare_admin_key_mints(http):
    assert (await _mint(http, headers={})).status_code == 401
    assert (await _mint(http, headers={"Authorization": "Bearer nope"})).status_code == 401
    await http.store.create_user(User("root", "Root", "admin"))
    _, token = await http.store.create_token("root")
    # An admin *user* (token, or admin key acting as them) cannot vouch for others.
    assert (await _mint(http, headers={"Authorization": f"Bearer {token}"})).status_code == 403
    acting = {**ADMIN, "X-Handler-User": "root"}
    assert (await _mint(http, headers=acting)).status_code == 403


@pytest.mark.parametrize("body", [{}, {"ticket": "short"}, {"ticket": "x" * 300},
                                  {"ticket": "a" * 43, "extra": 1}])
async def test_malformed_exchange(http, body):
    assert (await http.post("/login/ticket", json=body)).status_code == 422


async def test_unknown_ticket_is_401(http):
    response = await http.post("/login/ticket", json={"ticket": "a" * 43})
    assert response.status_code == 401


async def test_invalid_user_id_rejected(http):
    assert (await _mint(http, {**SITE_ADMIN, "user_id": "a:b"})).status_code == 422
    assert (await _mint(http, {**SITE_ADMIN, "role": "owner"})).status_code == 422


async def test_login_page_reads_and_strips_fragment(http):
    page = await http.get("/login")
    assert page.status_code == 200
    assert "location.hash" in page.text
    assert "history.replaceState" in page.text
    assert '"/login/ticket"' in page.text and 'method: "POST"' in page.text
    assert 'handlerPath("/app")' in page.text
    assert page.headers["cache-control"] == "no-store"
