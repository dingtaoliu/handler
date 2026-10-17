"""Authentication across the HTTP API."""

from __future__ import annotations

import asyncio
import logging
import socket
from urllib.parse import quote

import httpx
import pytest
import uvicorn
from fastapi.routing import APIRoute
from starlette.requests import Request

from handler.runtime.auth import Authenticator, Principal, can_see_thread
from handler.runtime.bus import BusMessage
from tests.runtime.auth.conftest import ADMIN_KEY, ctx


# --- credentials and the 401/403 matrix ---------------------------------------
async def test_healthz_login_open_and_timeline_authenticated(client, world):
    assert (await client.get("/healthz")).json() == {"ok": True}
    assert (await client.get("/timeline")).status_code == 401
    assert (await client.get("/login")).status_code == 200
    page = await client.get("/timeline", headers=world.as_user("alice"))
    assert page.status_code == 200
    assert "sessionStorage" in page.text and "access_token" in page.text
    assert "Authorization" in page.text


async def test_docs_and_openapi_are_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404


def _concrete(path: str) -> str:
    return path.replace("{key:path}", "chat:alice:c1").replace("{run_id}", "r").replace(
        "{approval_id}", "a").replace("{section}", "persona").replace(
        "{user_id}", "alice").replace("{token_id}", "t").replace("{id}", "n")


async def test_every_api_route_requires_credentials(client, app):
    checked = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api"):
            continue
        for method in route.methods:
            response = await client.request(method, _concrete(route.path), json={})
            assert response.status_code == 401, (method, route.path)
            assert response.headers["www-authenticate"] == "Bearer"
            bad = await client.request(method, _concrete(route.path), json={},
                                       headers={"Authorization": "Bearer nope"})
            assert bad.status_code == 401, (method, route.path)
            checked.append((method, route.path))
    assert len(checked) >= 25


@pytest.mark.parametrize("header", ["Basic abc", "Bearer", "Bearer ", "bearer"])
async def test_malformed_authorization_is_401(client, header):
    assert (await client.get("/api/me", headers={"Authorization": header})).status_code == 401


async def test_admin_only_routes_403_for_members(client, world):
    member = world.as_user("alice")
    calls = [
        ("PUT", "/api/settings/persona", {"system": "x"}),
        ("POST", "/api/settings/persona/rollback", {"version": 1}),
        ("POST", "/api/users", {"id": "eve", "display_name": "Eve"}),
        ("GET", "/api/users", None),
        ("PATCH", "/api/users/bob", {"disabled": True}),
        ("POST", "/api/users/bob/tokens", {"label": "x"}),
        ("GET", "/api/users/bob/tokens", None),
        ("DELETE", "/api/tokens/whatever", None),
        ("GET", "/api/audit", None),
    ]
    for method, path, body in calls:
        response = await client.request(method, path, json=body, headers=member)
        assert response.status_code == 403, (method, path)
        # ... and the same call is not blocked for admins.
        for admin in (world.as_user("root"), world.admin_key):
            response = await client.request(method, path, json=body, headers=admin)
            assert response.status_code != 403, (method, path)


async def test_any_user_can_read_settings_and_me(client, world):
    member = world.as_user("alice")
    assert (await client.get("/api/settings", headers=member)).status_code == 200
    assert (await client.get("/api/settings/persona/history", headers=member)).status_code == 200
    me = (await client.get("/api/me", headers=member)).json()
    assert me == {"user_id": "alice", "role": "member", "via": "user_token",
                  "display_name": "Alice"}


async def test_settings_writes_record_the_principal(client, world):
    response = await client.put("/api/settings/persona", json={"system": "s"},
                                headers=world.as_user("root"))
    assert response.json()["updated_by"] == "root"
    response = await client.put("/api/settings/persona", json={"system": "t"},
                                headers={**world.admin_key, "If-Match": "1"})
    assert response.json()["updated_by"] == "admin"


# --- admin key and X-Handler-User -----------------------------------------------
async def test_admin_key_principal_and_act_as(client, world):
    me = (await client.get("/api/me", headers=world.admin_key)).json()
    assert me == {"user_id": None, "role": "admin", "via": "admin_key", "display_name": None}
    acting = {**world.admin_key, "X-Handler-User": "alice"}
    me = (await client.get("/api/me", headers=acting)).json()
    assert (me["user_id"], me["role"], me["via"]) == ("alice", "member", "admin_key")
    for header in ("ghost",):
        response = await client.get("/api/me", headers={**world.admin_key,
                                                         "X-Handler-User": header})
        assert response.status_code == 403
    # Acting as a member means member permissions.
    assert (await client.get("/api/users", headers=acting)).status_code == 403


async def test_act_as_disabled_user_is_rejected(client, world, store):
    await store.update_user("alice", disabled=True)
    response = await client.get("/api/me", headers={**world.admin_key,
                                                     "X-Handler-User": "alice"})
    assert response.status_code == 403
    assert (await client.get("/api/me", headers=world.as_user("alice"))).status_code == 401


async def test_user_tokens_cannot_impersonate(client, world):
    response = await client.get("/api/me", headers={**world.as_user("alice"),
                                                     "X-Handler-User": "root"})
    assert response.json()["user_id"] == "alice"


async def test_wrong_admin_key_and_no_admin_key_configured(store, bus, world):
    for configured, presented in ((ADMIN_KEY, ADMIN_KEY[:-1]), (ADMIN_KEY, ADMIN_KEY + "x"),
                                  (None, ""), (None, "None"), ("", "")):
        auth = Authenticator(store, configured)
        assert await auth.authenticate(presented, None) is None


async def test_admin_key_compared_in_constant_time(store, monkeypatch):
    import handler.runtime.auth as auth_module

    seen = []
    real = auth_module.hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth_module.hmac, "compare_digest", spy)
    auth = Authenticator(store, ADMIN_KEY)
    assert await auth.authenticate(ADMIN_KEY, None) == Principal(None, "admin", "admin_key")
    assert await auth.authenticate("short", None) is None
    assert len(seen) == 2 and all(len(a) == len(b) == 32 for a, b in seen)


async def test_tokens_are_never_logged_or_echoed(client, world, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "super-secret-token-value"
    response = await client.get("/api/me", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 401 and secret not in response.text
    await client.get("/api/me", headers=world.as_user("alice"))
    await client.get("/api/me", headers=world.admin_key)
    everything = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    for value in (secret, ADMIN_KEY, *world.tokens.values()):
        assert value not in everything


# --- admin API -------------------------------------------------------------------
async def test_user_and_token_management(client, world):
    admin = world.as_user("root")
    created = await client.post("/api/users", headers=admin, json={
        "id": "carol", "display_name": "Carol", "role": "member"})
    assert created.status_code == 201 and created.json()["id"] == "carol"
    assert (await client.post("/api/users", headers=admin, json={
        "id": "carol", "display_name": "Again"})).status_code == 409
    for body in ({"id": "a:b", "display_name": "x"}, {"id": "ok", "display_name": "x",
                                                      "role": "owner"},
                 {"id": "ok", "display_name": "x", "extra": 1}):
        assert (await client.post("/api/users", headers=admin, json=body)).status_code == 422
    assert {u["id"] for u in (await client.get("/api/users", headers=admin)).json()} == {
        "root", "alice", "bob", "carol"}

    issued = await client.post("/api/users/carol/tokens", headers=admin, json={"label": "ci"})
    assert issued.status_code == 201
    token = issued.json()["token"]
    assert (await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
            ).json()["user_id"] == "carol"
    listed = (await client.get("/api/users/carol/tokens", headers=admin)).json()
    assert listed[0]["label"] == "ci" and listed[0]["last_used_at"] is not None
    assert "token" not in listed[0] and "token_hash" not in listed[0]
    assert (await client.post("/api/users/ghost/tokens", headers=admin, json={})
            ).status_code == 404

    patched = await client.patch("/api/users/carol", headers=admin,
                                 json={"role": "admin", "display_name": "Carol C"})
    assert patched.json()["role"] == "admin"
    await client.patch("/api/users/carol", headers=admin, json={"disabled": True})
    assert (await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
            ).status_code == 401
    await client.patch("/api/users/carol", headers=admin, json={"disabled": False})
    assert (await client.patch("/api/users/ghost", headers=admin, json={})).status_code == 404

    assert (await client.delete(f"/api/tokens/{issued.json()['token_id']}", headers=admin)
            ).json() == {"revoked": True}
    assert (await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
            ).status_code == 401
    assert (await client.delete(f"/api/tokens/{issued.json()['token_id']}", headers=admin)
            ).status_code == 404


# --- chat ------------------------------------------------------------------------
async def test_chat_user_comes_from_the_principal(client, world, store):
    body = {"text": "hi", "conversation_id": "c9", "user_id": "bob"}
    response = await client.post("/api/chat", json=body, headers=world.as_user("alice"))
    assert response.json()["thread_key"] == "chat:alice:c9"
    thread = await store.get_thread("chat:alice:c9")
    assert thread.owner_user_id == "alice"
    assert await store.get_thread("chat:bob:c9") is None


async def test_chat_as_admin_needs_a_user(client, world):
    body = {"text": "hi", "conversation_id": "c2"}
    assert (await client.post("/api/chat", json=body, headers=world.admin_key)
            ).status_code == 400
    acting = {**world.admin_key, "X-Handler-User": "bob"}
    assert (await client.post("/api/chat", json=body, headers=acting)
            ).json()["thread_key"] == "chat:bob:c2"
    # An admin user with their own token chats as themselves.
    assert (await client.post("/api/chat", json=body, headers=world.as_user("root"))
            ).json()["thread_key"] == "chat:root:c2"


# --- visibility ------------------------------------------------------------------
def test_can_see_thread_rules():
    alice = Principal("alice", "member", "user_token")
    assert can_see_thread(alice, "chat:alice:1")
    assert not can_see_thread(alice, "chat:bob:1")
    assert not can_see_thread(alice, "chat:alicia:1") and not can_see_thread(alice, "chat:alice")
    assert can_see_thread(alice, "gmail:inbox") and can_see_thread(alice, "automation:x")
    assert can_see_thread(Principal("root", "admin", "user_token"), "chat:bob:1")
    assert can_see_thread(Principal(None, "admin", "admin_key"), "chat:bob:1")


THREAD_ROUTES = ["/api/threads/{k}", "/api/threads/{k}/messages", "/api/threads/{k}/runs",
                 "/api/threads/{k}/stream"]


@pytest.mark.parametrize("route", THREAD_ROUTES)
async def test_members_cannot_read_other_users_chats(client, world, route):
    bob_chat = route.format(k="chat:bob:c1")
    assert (await client.get(bob_chat, headers=world.as_user("alice"))).status_code == 404
    # The same answer as for a thread that doesn't exist: no key probing.
    missing = route.format(k="chat:bob:nope")
    assert (await client.get(missing, headers=world.as_user("alice"))).status_code == 404
    # SSE routes are exercised against a live server below; skip the open-ended ones here.
    if not route.endswith("/stream"):
        assert (await client.get(bob_chat, headers=world.as_user("bob"))).status_code == 200
        assert (await client.get(bob_chat, headers=world.as_user("root"))).status_code == 200
        assert (await client.get(bob_chat, headers=world.admin_key)).status_code == 200


async def test_members_read_own_and_shared_threads(client, world):
    alice = world.as_user("alice")
    for key in ("chat:alice:c1", "gmail:inbox"):
        for suffix in ("", "/messages", "/runs"):
            response = await client.get(f"/api/threads/{key}{suffix}", headers=alice)
            assert response.status_code == 200, (key, suffix)
    messages = (await client.get("/api/threads/chat:alice:c1/messages", headers=alice)).json()
    assert messages[0]["blocks"][0]["text"] == "chat:alice:c1"


async def test_run_detail_respects_thread_visibility(client, world):
    alice = world.as_user("alice")
    assert (await client.get("/api/runs/run-chat:alice:c1", headers=alice)).status_code == 200
    assert (await client.get("/api/runs/run-gmail:inbox", headers=alice)).status_code == 200
    assert (await client.get("/api/runs/run-chat:bob:c1", headers=alice)).status_code == 404
    assert (await client.get("/api/runs/run-chat:bob:c1", headers=world.as_user("root"))
            ).status_code == 200


async def test_thread_listing_is_filtered(client, world):
    def keys(response):
        return {t["key"] for t in response.json()}

    alice = await client.get("/api/threads", headers=world.as_user("alice"))
    assert keys(alice) == {"chat:alice:c1", "gmail:inbox"}
    peek = await client.get("/api/threads", params={"user_id": "bob"},
                            headers=world.as_user("alice"))
    assert keys(peek) == set()
    everything = await client.get("/api/threads", headers=world.as_user("root"))
    assert keys(everything) == {"chat:alice:c1", "chat:bob:c1", "gmail:inbox"}
    assert keys(await client.get("/api/threads", headers=world.admin_key)) == keys(everything)
    scoped = {**world.admin_key, "X-Handler-User": "bob"}
    assert keys(await client.get("/api/threads", headers=scoped)) == {"chat:bob:c1",
                                                                      "gmail:inbox"}


# --- approvals and notifications -------------------------------------------------
async def test_approvals_are_scoped_but_any_member_may_decide_visible_ones(
    client, world, gateway
):
    private = (await gateway.call(ctx("chat:alice:c1"), "send_it", {})).content["approval_id"]
    shared = (await gateway.call(ctx("gmail:inbox"), "send_it", {})).content["approval_id"]
    bob = world.as_user("bob")
    listed = (await client.get("/api/approvals", headers=bob)).json()
    assert [a["id"] for a in listed] == [shared]
    assert (await client.get(f"/api/approvals/{private}", headers=bob)).status_code == 404
    for action in ("approve", "deny"):
        assert (await client.post(f"/api/approvals/{private}/{action}", json={},
                                  headers=bob)).status_code == 404
    done = await client.post(f"/api/approvals/{shared}/approve", json={"decided_by": "root"},
                             headers=bob)
    assert done.status_code == 200 and done.json()["decided_by"] == "bob"  # not spoofable
    alice = world.as_user("alice")
    denied = await client.post(f"/api/approvals/{private}/deny", json={}, headers=alice)
    assert denied.json()["status"] == "denied"
    assert len((await client.get("/api/approvals", headers=world.as_user("root"))).json()) == 2


async def test_notifications_are_private(client, world, store):
    mine = await store.add_notification("alice", "gmail:inbox", None, "for alice")
    theirs = await store.add_notification("bob", "gmail:inbox", None, "for bob")
    alice = world.as_user("alice")
    assert [n["id"] for n in (await client.get("/api/notifications", headers=alice)).json()
            ] == [mine.id]
    assert (await client.get("/api/notifications", params={"user_id": "bob"},
                             headers=alice)).status_code == 403
    assert (await client.post(f"/api/notifications/{theirs.id}/read", headers=alice)
            ).status_code == 404
    assert (await client.post(f"/api/notifications/{mine.id}/read", headers=alice)
            ).status_code == 200
    root = world.as_user("root")
    assert len((await client.get("/api/notifications", params={"user_id": "bob"},
                                 headers=root)).json()) == 1
    assert (await client.get("/api/notifications", headers=world.admin_key)
            ).status_code == 400


# --- SSE ---------------------------------------------------------------------------
def _request(path: str, query: str = "", headers: dict[str, str] | None = None) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query.encode(),
                    "headers": [(k.lower().encode(), v.encode())
                                for k, v in (headers or {}).items()]})


async def test_access_token_only_on_sse_dependencies(store, world):
    auth = Authenticator(store, ADMIN_KEY)
    token = world.tokens["alice"]
    query = f"access_token={token}"
    assert (await auth.require_sse_principal(_request("/api/stream", query))).user_id == "alice"
    with pytest.raises(Exception) as excinfo:
        await auth.require_principal(_request("/api/me", query))
    assert excinfo.value.status_code == 401
    # The header still wins and works on SSE routes too.
    header = {"Authorization": f"Bearer {token}"}
    assert (await auth.require_sse_principal(_request("/api/stream", headers=header))
            ).user_id == "alice"
    admin_query = f"access_token={ADMIN_KEY}"
    assert (await auth.require_sse_principal(_request("/api/stream", admin_query))
            ).via == "admin_key"


async def test_access_token_rejected_on_regular_routes(client, world):
    token = world.tokens["alice"]
    for path in ("/api/me", "/api/threads", "/api/threads/chat:alice:c1/messages",
                 "/api/runs/run-chat:alice:c1", "/api/users"):
        response = await client.get(path, params={"access_token": token})
        assert response.status_code == 401, path


async def test_sse_routes_reject_bad_tokens_and_foreign_threads(client, world):
    assert (await client.get("/api/stream")).status_code == 401
    assert (await client.get("/api/stream", params={"access_token": "bad"})).status_code == 401
    other = quote("chat:bob:c1", safe=":")
    response = await client.get(f"/api/threads/{other}/stream",
                                params={"access_token": world.tokens["alice"]})
    assert response.status_code == 404


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _frames(response, count: int, seen: list[str]) -> None:
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            seen.append(line[6:])
            if len(seen) >= count:
                return


async def test_live_sse_stream_only_carries_visible_threads(app, world, bus):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(timeout=5) as http:
            seen: list[str] = []
            async with http.stream("GET", f"{url}/api/stream",
                                   params={"access_token": world.tokens["alice"]}) as response:
                assert response.status_code == 200
                await asyncio.sleep(0.2)  # let the subscription register
                for key in ("chat:bob:c1", "chat:alice:c1", "chat:bob:c1", "gmail:inbox"):
                    bus.publish(BusMessage(key, None, "event_queued", {"k": key}))
                await asyncio.wait_for(_frames(response, 2, seen), 5)
            keys = [__import__("json").loads(frame)["thread_key"] for frame in seen]
            assert keys == ["chat:alice:c1", "gmail:inbox"]

            # An admin gets everything, and a wildcard thread stream can't be abused.
            seen = []
            wild = quote("*", safe="")
            async with http.stream("GET", f"{url}/api/threads/{wild}/stream",
                                   params={"access_token": world.tokens["alice"]}) as response:
                await asyncio.sleep(0.2)
                bus.publish(BusMessage("chat:bob:c1", None, "event_queued", {}))
                bus.publish(BusMessage("chat:alice:c1", None, "event_queued", {}))
                await asyncio.wait_for(_frames(response, 1, seen), 5)
            assert __import__("json").loads(seen[0])["thread_key"] == "chat:alice:c1"
            seen = []
            async with http.stream("GET", f"{url}/api/stream", params={
                "access_token": world.tokens["root"]}) as response:
                await asyncio.sleep(0.2)
                bus.publish(BusMessage("chat:bob:c1", None, "event_queued", {}))
                await asyncio.wait_for(_frames(response, 1, seen), 5)
            assert __import__("json").loads(seen[0])["thread_key"] == "chat:bob:c1"
    finally:
        server.should_exit = True
        await task


async def test_chat_idempotency_is_scoped_to_user(client, world):
    body = {"text": "hello", "client_message_id": "same-client-id"}
    alice = await client.post("/api/chat", json=body, headers=world.as_user("alice"))
    bob = await client.post("/api/chat", json=body, headers=world.as_user("bob"))
    assert not alice.json()["duplicate"] and not bob.json()["duplicate"]
    retry = await client.post("/api/chat", json=body, headers=world.as_user("alice"))
    assert retry.json()["duplicate"]


async def test_sse_act_as_keeps_member_visibility(store, world):
    auth = Authenticator(store, ADMIN_KEY)
    request = _request("/api/stream", f"access_token={ADMIN_KEY}&act_as=alice")
    principal = await auth.require_sse_principal(request)
    assert principal == Principal("alice", "member", "admin_key")
    assert not can_see_thread(principal, "chat:bob:c1")
    request = _request("/api/stream", f"access_token={world.tokens['alice']}&act_as=root")
    assert (await auth.require_sse_principal(request)).user_id == "alice"


async def test_open_stream_stops_after_revocation(store, world, bus):
    from handler.runtime.web.app import sse_stream

    auth = Authenticator(store, ADMIN_KEY)
    request = _request("/api/stream", f"access_token={world.tokens['alice']}")

    async def authorized():
        try:
            await auth.require_sse_principal(request)
            return True
        except Exception:
            return False

    stream = sse_stream(bus, "*", authorized=authorized)
    assert await anext(stream) == ": connected\n\n"
    await store.revoke_token(world.token_ids["alice"])
    bus.publish(BusMessage("chat:alice:c1", None, "event_queued", {}))
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
