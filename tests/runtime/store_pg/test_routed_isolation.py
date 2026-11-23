"""Isolation of the host's one routed app (role-split-design.md, "Isolation": routing).

The tenant comes from the request's credentials; `/i/<name>` (or the root alias for
main) is only a hint. Every check runs on the env-defined host and the registry host,
on a real loopback port, with users of the same id in two tenants.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from handler.runtime.context import RunContext

from .production_shape import admin_key, free_port, live, patch_runtime, production_env, registry_on

PREFIXES = {"main": ["", "/i/main"], "bss": ["/i/bss"], "yeup": ["/i/yeup"]}
MCP = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def bearer(token: str, **extra) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


@pytest.fixture(params=["env", "registry"])
async def host(request, tmp_path, pg_dsn, monkeypatch):
    patch_runtime(monkeypatch)
    env = production_env(tmp_path, pg_dsn, free_port())
    for name in ("main", "bss", "yeup"):  # no Crontal reachable here
        env.pop("CRONTAL_URL" if name == "main" else f"HANDLER_I_{name.upper()}__CRONTAL_URL")
    if request.param == "registry":
        env = registry_on(env)
    async with live(env) as (app, client):
        tokens = {}
        for name in PREFIXES:
            prefix = PREFIXES[name][-1]
            created = await client.post(f"{prefix}/api/users", headers=bearer(admin_key(name)),
                                        json={"id": "alice", "display_name": "Alice",
                                              "role": "member"})
            assert created.status_code == 201, created.text
            token = await client.post(f"{prefix}/api/users/alice/tokens",
                                      headers=bearer(admin_key(name)), json={"label": "t"})
            tokens[name] = token.json()["token"]
        yield app, client, tokens


async def test_admin_keys_and_user_tokens_only_open_their_own_tenant(host):
    _, client, tokens = host
    for owner in PREFIXES:
        for credential in (admin_key(owner), tokens[owner]):
            for tenant, prefixes in PREFIXES.items():
                for prefix in prefixes:
                    for path in ("/api/me", "/api/threads", "/api/uploads"):
                        response = await client.get(prefix + path, headers=bearer(credential))
                        expected = 200 if tenant == owner else 401
                        assert response.status_code == expected, (owner, prefix, path)
                    # SSE accepts ?access_token=; the same rule holds.
                    async with client.stream(
                            "GET", f"{prefix}/api/threads/chat:alice:c1/stream"
                                   f"?access_token={credential}") as response:
                        expected = 200 if tenant == owner else 401
                        assert response.status_code == expected, (owner, prefix)


async def test_a_users_threads_uploads_and_streams_stay_in_their_tenant(host):
    app, client, tokens = host
    main, bss = bearer(tokens["main"]), bearer(tokens["bss"])
    # alice@bss watches the thread key alice@main is about to use.
    seen: list[dict] = []

    async def watch():
        async with client.stream("GET", "/i/bss/api/threads/chat:alice:c1/stream",
                                 headers=bss) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    seen.append(json.loads(line[6:]))

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0.2)
    upload = await client.post("/api/uploads", headers=main,
                               files=[("files", ("plan.txt", b"main only", "text/plain"))])
    file_id = upload.json()[0]["id"]
    sent = await client.post("/api/chat", headers=main, json={
        "text": "for main only", "conversation_id": "c1", "attachments": [file_id]})
    key = sent.json()["thread_key"]
    assert key == "chat:alice:c1"

    async def answered():
        while True:
            messages = (await client.get(f"/i/main/api/threads/{key}/messages", headers=main)).json()
            if any(m["role"] == "assistant" for m in messages):
                return
            await asyncio.sleep(0.05)
    await asyncio.wait_for(answered(), 15)
    await asyncio.sleep(0.3)
    watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)
    assert seen == []  # bss's stream for the same key carried none of main's run

    # Under both of main's URLs main sees it; bss, with the same user id, does not.
    for prefix in PREFIXES["main"]:
        threads = (await client.get(f"{prefix}/api/threads", headers=main)).json()
        assert key in {t["key"] for t in threads}
        assert (await client.get(f"{prefix}/api/uploads/{file_id}", headers=main)).status_code == 200
        for credential in (bss, bearer(admin_key("bss"))):
            assert (await client.get(f"{prefix}/api/threads/{key}/messages",
                                     headers=credential)).status_code == 401
            assert (await client.get(f"{prefix}/api/uploads/{file_id}",
                                     headers=credential)).status_code == 401
    assert key not in {t["key"] for t in (await client.get("/i/bss/api/threads", headers=bss)).json()}
    assert (await client.get(f"/i/bss/api/threads/{key}/messages", headers=bss)).json() == []
    assert (await client.get(f"/i/bss/api/uploads/{file_id}", headers=bss)).status_code == 404
    assert (await client.get(f"/i/bss/api/uploads/{file_id}",
                             headers=bearer(admin_key("bss")))).status_code == 404


async def test_run_tokens_reach_only_their_tenants_gateway(host):
    app, client, _ = host
    services = app.state.services
    issued = {}
    for name in PREFIXES:
        async with services.tenants.use(name) as context:
            issued[name] = services.tokens.issue(
                RunContext(f"run-{name}", "chat:alice:c1", "alice"), 60,
                gateway=context.gateway, tenant=name)
    accept = {"Accept": "application/json, text/event-stream"}
    for owner, token in issued.items():
        root = await client.post("/mcp/", json=MCP, headers=bearer(token, **accept))
        assert root.status_code == 200, owner  # the host route: the token alone
        for tenant in PREFIXES:
            response = await client.post(f"/i/{tenant}/mcp/", json=MCP,
                                         headers=bearer(token, **accept))
            assert response.status_code == (200 if tenant == owner else 401), (owner, tenant)
    # A tenant's admin key or a made-up token is no run token.
    for credential in (admin_key("main"), "made-up"):
        response = await client.post("/mcp/", json=MCP, headers=bearer(credential, **accept))
        assert response.status_code == 401


async def test_sign_in_tickets_and_webhooks_are_bound_to_their_tenant(host):
    _, client, _ = host
    ticket = await client.post("/api/auth/sign-in-tickets", headers=bearer(admin_key("main")),
                               json={"user_id": "alice", "display_name": "Alice",
                                     "role": "member"})
    value = ticket.json()["ticket"]
    assert (await client.post("/i/bss/login/ticket", json={"ticket": value})).status_code == 401
    assert (await client.post("/i/main/login/ticket", json={"ticket": value})).status_code == 200
    from handler.runtime.web.ingest import sign

    from .production_shape import webhook_secret

    note = json.dumps({"id": 5, "recipient_user_id": "alice"}).encode()
    for tenant in ("bss", "yeup"):
        response = await client.post(f"/i/{tenant}/ingest/crontal", content=note, headers={
            "X-Crontal-Signature": sign(webhook_secret("main"), note)})
        assert response.status_code == 401, tenant
    response = await client.post("/i/main/ingest/crontal", content=note, headers={
        "X-Crontal-Signature": sign(webhook_secret("main"), note)})
    assert response.status_code == 202


async def test_unknown_tenants_and_disabled_routes_are_404(host):
    _, client, _ = host
    for path in ("/i/nope/api/me", "/i/nope/healthz", "/i/nope/mcp/", "/i/nope/app"):
        assert (await client.get(path, headers=bearer(admin_key("main")))).status_code == 404
    # A route group a tenant isn't served (mailbox is BSS's integration only).
    assert (await client.get("/i/yeup/api/mailboxes",
                             headers=bearer(admin_key("yeup")))).status_code == 404
    assert (await client.get("/i/bss/api/mailboxes",
                             headers=bearer(admin_key("bss")))).status_code == 200
    # Public pages resolve the tenant by the hint and need no credential.
    for prefix in ("", "/i/bss"):
        page = await client.get(f"{prefix}/login")
        assert page.status_code == 200 and "HANDLER_CONFIG" in page.text
    assert (await client.get("/i/bss/healthz")).json() == {"ok": True}
