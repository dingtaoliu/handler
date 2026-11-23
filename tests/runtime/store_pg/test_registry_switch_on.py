"""Runbook stage 1 rehearsed: production's env-defined instances, then the registry on.

The database starts as production has it (env-defined `main`, `bss` and `yeup`, each
with users, tokens, chats, schedules, memory, an approval and, for BSS, a mailbox).
Then `HANDLER_TENANT_REGISTRY=1` with the platform and sealing keys, on a real
loopback port as production serves it, and Crontal's request shapes
(handler_service.py / handler_controller.py in Crontal): admin key with
`X-Handler-User`, under `/i/<name>` base URLs. Then a free tenant made through the
platform API, and the registry switched off again.

The operator procedure is in docs/rebuild/tenant-registry.md ("Switching the
registry on in production").
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import psycopg
import pytest

from handler.runtime import server
from handler.runtime.gateway.secret_box import SecretBox
from handler.runtime.integrations.mailbox import transport as transport_module
from handler.runtime.types import Approval
from handler.runtime.web.ingest import sign

from ..integrations.mailbox.fake_mail import FakeMailServer, FakeTransport
from .production_shape import (
    CRONTAL_URL,
    MERCHANTS,
    PLATFORM_KEY,
    PREFIXES,
    SCHEMAS,
    SEALING_KEY,
    FakeHarness,
    admin_key,
    free_port,
    live,
    patch_runtime,
    production_env,
    registry_on,
    webhook_secret,
)

PLATFORM = {"Authorization": f"Bearer {PLATFORM_KEY}"}
MAILBOX = {"label": "Sales", "email": "sales@bss.example", "password": "imap-pass-81",
           "imap_host": "imap.bss.example", "imap_port": 993, "imap_security": "tls",
           "smtp_host": "smtp.bss.example", "smtp_port": 465, "smtp_security": "tls",
           "wake_mode": "off"}
# Tables whose rows are the tenants' data; none may change when the registry imports.
DATA_TABLES = ("users", "user_tokens", "threads", "messages", "events", "runs", "run_steps",
               "schedules", "memory_topics", "approvals", "settings_versions", "connections",
               "secrets", "audit_log", "notifications", "deliveries", "mail_accounts",
               "mail_folders", "mail_messages")


def base(name: str) -> str:
    return "" if name == "main" else f"/i/{name}"


def as_user(name: str, user: str) -> dict[str, str]:
    """Crontal's proxy headers (handler_service._admin_headers)."""
    return {"Authorization": f"Bearer {admin_key(name)}", "X-Handler-User": user}


def bare(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_key(name)}"}


class FakeCrontal:
    """Crontal's agent-mcp listing, for the host's discovery cache."""

    down: set[str] = set()  # tokens whose discovery fails
    listed: list[str] = []

    def __init__(self, url, token):
        self.url, self.token = url, token

    async def discover(self):
        FakeCrontal.listed.append(self.token)
        if self.token in FakeCrontal.down:
            raise ConnectionError("crontal unreachable")
        from handler.runtime.gateway.crontal import CRONTAL_TOOLS

        tool = type("Tool", (), {})
        tools = {}
        for name in CRONTAL_TOOLS:
            tools[name] = tool()
            tools[name].name, tools[name].description = name, name
            tools[name].inputSchema = {"type": "object", "properties": {}}
        return tools

    def adopt(self, tools):
        from handler.runtime.gateway.crontal import crontal_connector

        self.real = crontal_connector(self.url, self.token)
        self.real.adopt(tools)

    def tools(self):
        return self.real.tools()

    @property
    def name(self):
        return "crontal"


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    patch_runtime(monkeypatch)
    FakeCrontal.down, FakeCrontal.listed = set(), []
    real = server.CrontalDiscovery

    def discovery(**kwargs):
        return real(connect=lambda url, token: FakeCrontal(url, token), **kwargs)

    monkeypatch.setattr(server, "CrontalDiscovery", discovery)
    mail = FakeMailServer(username=MAILBOX["email"], password=MAILBOX["password"])
    monkeypatch.setattr(transport_module, "StdlibTransport", lambda: FakeTransport(mail))


def rows(dsn, query, *params):
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()


def snapshot(dsn) -> dict[str, dict[str, int]]:
    """Row counts of every tenant data table, per schema."""
    out = {}
    for name, schema in SCHEMAS.items():
        counts = {}
        for table in DATA_TABLES:
            found = rows(dsn, "SELECT to_regclass(%s)", f"{schema}.{table}")[0][0]
            if found is not None:
                counts[table] = rows(dsn, f'SELECT count(*) FROM "{schema}"."{table}"')[0][0]
        out[name] = counts
    return out


async def until(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.05)


async def chat(client, name, user, text, conversation):
    response = await client.post(f"{base(name)}/api/chat", headers=as_user(name, user),
                                 json={"text": text, "conversation_id": conversation,
                                       "client_message_id": f"{conversation}:{text}"})
    assert response.status_code == 200, response.text
    return response.json()


async def answered(client, name, user, key, count=1, headers=None):
    async def done():
        response = await client.get(f"{base(name)}/api/threads/{key}/messages",
                                    headers=headers or as_user(name, user))
        assert response.status_code == 200, response.text
        replies = [m for m in response.json() if m["role"] == "assistant"]
        return replies if len(replies) >= count else None
    return await until(done)


async def seed(client, app):
    """What production has in each instance before the switch."""
    tokens = {}
    for name in SCHEMAS:
        user = f"{name}-owner"
        created = await client.post(f"{base(name)}/api/users", headers=bare(name),
                                    json={"id": user, "display_name": user, "role": "admin"})
        assert created.status_code == 201, created.text
        token = await client.post(f"{base(name)}/api/users/{user}/tokens", headers=bare(name),
                                  json={"label": "panel"})
        tokens[name] = token.json()["token"]
        reply = await chat(client, name, user, f"hello from {name}", "c1")
        await answered(client, name, user, reply["thread_key"])
        automation = await client.post(f"{base(name)}/api/automations", headers=as_user(name, user),
                                       json={"name": "daily", "prompt": "summarise",
                                             "schedule": "every 1d"})
        assert automation.status_code in (200, 201), automation.text
        memory = await client.put(f"{base(name)}/api/memory/company", headers=bare(name),
                                  json={"content": f"{name} buys 304 sheet"})
        assert memory.status_code == 200, memory.text
        async with app.state.services.tenants.use(name) as context:
            await context.store.create_approval(Approval(
                run_id="run_seed", thread_key=reply["thread_key"], tool="crontal_create_rfq",
                action="crontal.create_rfq", args={"title": "seed"}, requested_by=user,
                id=f"apr_{name}"))
    mailbox = await client.post("/i/bss/api/mailboxes", headers=bare("bss"), json=MAILBOX)
    assert mailbox.status_code == 201, mailbox.text
    return tokens


async def sees_everything(client, tokens, *, imported_chat: bool = False):
    """Existing URLs, keys and tokens reach the same data."""
    for name in SCHEMAS:
        prefixes = ["", "/i/main"] if name == "main" else [base(name)]
        for prefix in prefixes:
            me = await client.get(f"{prefix}/api/me", headers=bare(name))
            assert me.status_code == 200 and me.json()["via"] == "admin_key", (prefix, me.text)
            me = await client.get(f"{prefix}/api/me",
                                  headers={"Authorization": f"Bearer {tokens[name]}"})
            assert me.json()["user_id"] == f"{name}-owner", prefix
        user = f"{name}-owner"
        threads = await client.get(f"{base(name)}/api/threads", headers=as_user(name, user))
        assert "chat:%s:c1" % user in {t["key"] for t in threads.json()}
        schedules = await client.get(f"{base(name)}/api/automations", headers=bare(name))
        assert [s["name"] for s in schedules.json()] == ["daily"]
        memory = await client.get(f"{base(name)}/api/memory/company", headers=bare(name))
        assert memory.json()["content"] == f"{name} buys 304 sheet"
        approvals = await client.get(f"{base(name)}/api/approvals", headers=bare(name))
        assert f"apr_{name}" in {a["id"] for a in approvals.json()}
    mailboxes = await client.get("/i/bss/api/mailboxes", headers=bare("bss"))
    assert [m["email"] for m in mailboxes.json()["mailboxes"]] == [MAILBOX["email"]]
    for name in ("main", "yeup"):  # mailbox is BSS's integration only
        assert (await client.get(f"{base(name)}/api/mailboxes",
                                 headers=bare(name))).status_code == 404


async def crontal_proxy_calls(client, app):
    """Every call shape Crontal's Handler proxy makes, against BSS."""
    user = "bss-owner"
    headers = as_user("bss", user)
    # Provisioning (handler_service.provision_user / sync_role / console_sign_in_url).
    assert (await client.post("/i/bss/api/users", headers=bare("bss"), json={
        "id": "17", "display_name": "Eric", "role": "member"})).status_code == 201
    assert (await client.patch("/i/bss/api/users/17", headers=bare("bss"),
                               json={"role": "member"})).status_code == 200
    ticket = await client.post("/i/bss/api/auth/sign-in-tickets", headers=bare("bss"),
                               json={"user_id": "17", "display_name": "Eric", "role": "member"})
    assert ticket.status_code == 201, ticket.text
    redeemed = await client.post("/i/bss/login/ticket", json={"ticket": ticket.json()["ticket"]})
    assert redeemed.status_code == 200 and redeemed.json()["user_id"] == "17"
    # Panel reads.
    for path, params in (("/api/limits/inputs", None), ("/api/chat/options", None),
                         ("/api/threads", {"exclude_type": "automation"}), ("/api/tasks", None),
                         ("/api/settings", None), ("/api/approvals", None),
                         ("/api/automations/dashboard", None)):
        response = await client.get(f"/i/bss{path}", headers=headers, params=params)
        assert response.status_code == 200, (path, response.text)
    # Upload (multipart, as Crontal re-sends it), then chat with it.
    upload = await client.post("/i/bss/api/uploads", headers=headers,
                               files=[("files", ("quote.txt", b"304 2B 2mm x 5t", "text/plain"))])
    assert upload.status_code == 200, upload.text
    file_id = upload.json()[0]["id"]
    fetched = await client.get(f"/i/bss/api/uploads/{file_id}", headers=headers)
    assert fetched.status_code == 200 and fetched.headers["X-Handler-Upload-Owner"] == user
    # The stream Crontal passes through (SSE, admin key + X-Handler-User).
    key = f"chat:{user}:c2"
    seen: list[dict] = []

    async def stream():
        async with client.stream("GET", f"/i/bss/api/threads/{key}/stream",
                                 headers=headers) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    seen.append(json.loads(line[6:]))
                    if seen[-1]["type"] == "run_finished":
                        return

    reader = asyncio.create_task(stream())
    await asyncio.sleep(0.2)
    sent = await client.post("/i/bss/api/chat", headers=headers, json={
        "text": "quote this", "conversation_id": "c2", "attachments": [file_id],
        "client_message_id": "c2-1", "context_links": ["crontal:rfq:77"]})
    assert sent.status_code == 200 and sent.json()["thread_key"] == key
    await asyncio.wait_for(reader, 15)
    assert {"event_queued", "run_started", "run_finished"} <= {m["type"] for m in seen}
    # A resend of the same message is reported as a duplicate of its thread.
    again = await client.post("/i/bss/api/chat", headers=headers, json={
        "text": "quote this", "conversation_id": "c2", "client_message_id": "c2-1"})
    assert again.json()["duplicate"] is True
    # The run used the host's one MCP route with its own token, and saw BSS's tools.
    run = next(r for r in FakeHarness.runs if r["prompt"].endswith("quote this")
               or "quote this" in r["prompt"])
    assert run["mcp"] == [f"http://127.0.0.1:{client.base_url.port}/mcp/"]
    assert "crontal_get_rfqs" in run["tools"] and "mail_search" in run["tools"]
    # Retry of a failed run (handler_controller retry_run).
    failed = await chat(client, "bss", user, "FAIL please", "c3")
    runs_path = f"/i/bss/api/threads/{failed['thread_key']}/runs"

    async def failed_run():
        listed = (await client.get(runs_path, headers=headers)).json()
        return listed[0] if listed and listed[0]["retryable"] else None
    run_row = await until(failed_run)
    FakeHarness.failing = False
    retried = await client.post(f"/i/bss/api/runs/{run_row['id']}/retry", headers=headers, json={})
    assert retried.status_code == 202, retried.text
    await answered(client, "bss", user, failed["thread_key"])
    # Crontal's webhook (outbox deliver_pending): signed body to /ingest/crontal.
    note = json.dumps({"id": 901, "type": "bid_received", "recipient_user_id": user,
                       "entity_type": "rfq", "entity_id": 77}).encode()
    hook = await client.post("/i/bss/ingest/crontal", content=note, headers={
        "Content-Type": "application/json",
        "X-Crontal-Signature": sign(webhook_secret("bss"), note)})
    assert hook.status_code == 202, hook.text
    # Another tenant's secret is refused.
    hook = await client.post("/i/yeup/ingest/crontal", content=note, headers={
        "Content-Type": "application/json",
        "X-Crontal-Signature": sign(webhook_secret("bss"), note)})
    assert hook.status_code == 401


async def test_registry_switch_on_rehearsal(tmp_path, pg_dsn, caplog):
    port = free_port()
    env = production_env(tmp_path, pg_dsn, port)

    # --- production today: env-defined instances --------------------------------
    async with live(env) as (app, client):
        assert not hasattr(app.state, "registry")
        tokens = await seed(client, app)
    before = snapshot(pg_dsn)
    assert all(before[name]["threads"] >= 1 for name in SCHEMAS)
    assert before["bss"]["mail_accounts"] == 1

    # --- stage 1: the registry on ------------------------------------------------
    caplog.set_level(logging.INFO)
    on = registry_on(env)
    async with live(on) as (app, client):
        # The three instances were imported unchanged (and their data untouched).
        assert snapshot(pg_dsn) == before
        listed = (await client.get("/platform/tenants", headers=PLATFORM)).json()
        by_name = {t["name"]: t for t in listed}
        assert set(by_name) == set(SCHEMAS)
        for name, tenant in by_name.items():
            assert tenant["merchant_id"] == MERCHANTS[name]
            assert tenant["schema"] == SCHEMAS[name]
            assert tenant["s3_prefix"] == PREFIXES[name]
            assert tenant["plan"] == "paid" and tenant["enabled"] is True
            assert admin_key(name) not in json.dumps(tenant)  # never a secret's value
        assert by_name["bss"]["integrations"] == ["crontal", "mailbox"]
        assert by_name["main"]["integrations"] is None  # unset: legacy wiring, as today
        registry = app.state.registry
        stored = {t.name: t for t in await registry.list()}
        box = SecretBox(SEALING_KEY)
        for name in SCHEMAS:
            secrets = json.loads(box.decrypt(stored[name].secrets))
            assert secrets["HANDLER_ADMIN_KEY"] == admin_key(name)
            assert secrets["HANDLER_WEBHOOK_SECRET"] == webhook_secret(name)
            assert secrets["CRONTAL_AGENT_TOKEN"] == f"{name}-agent-token"
            assert stored[name].config.get("CRONTAL_URL", env["CRONTAL_URL"]) == CRONTAL_URL
        main_secrets = json.loads(box.decrypt(stored["main"].secrets))
        assert main_secrets["GOOGLE_OAUTH_CLIENT_ID"] == "main-google-client"
        assert main_secrets["GOOGLE_OAUTH_CLIENT_SECRET"] == "main-google-secret"
        assert json.loads(box.decrypt(stored["bss"].secrets))["HANDLER_EMAIL_SECRET"] \
            == "bss-email-secret"
        assert stored["bss"].config["HANDLER_MAX_CONCURRENT"] == "5"
        assert stored["bss"].config["HANDLER_PUBLIC_URL"] == "https://handler.example/i/bss"

        # Existing URLs, keys and tokens keep working, with the same data.
        await sees_everything(client, tokens)
        # A key is refused on another tenant's URL, and on main's root alias.
        assert (await client.get("/i/bss/api/me", headers=bare("main"))).status_code == 401
        assert (await client.get("/api/me", headers=bare("yeup"))).status_code == 401
        await crontal_proxy_calls(client, app)
        assert any(r.message == "crontal tools registered" and r.tenant == "bss"
                   for r in caplog.records)
        assert app.state.services.crontal_discovery.fetches == 1  # once for the host

        # --- a free tenant on first use (stage 4's provisioning) -------------------
        FakeCrontal.down = {"m500-agent-token"}
        FakeCrontal.listed.clear()
        app.state.services.crontal_discovery._listings.clear()  # force a fresh look
        created = await client.put("/platform/tenants/500", headers=PLATFORM, json={
            "plan": "free", "integrations": ["crontal"],
            "config": {"CRONTAL_URL": CRONTAL_URL},
            "secrets": {"CRONTAL_AGENT_TOKEN": "m500-agent-token",
                        "HANDLER_WEBHOOK_SECRET": "m500-webhook"}})
        assert created.status_code == 201, created.text
        tenant, key = created.json(), created.json()["admin_key"]
        assert (tenant["name"], tenant["schema"], tenant["plan"]) == ("m500", "handler_m500", "free")
        free = {"Authorization": f"Bearer {key}", "X-Handler-User": "42"}
        assert (await client.post("/i/m500/api/users", headers={"Authorization": f"Bearer {key}"},
                                  json={"id": "42", "display_name": "Ann",
                                        "role": "admin"})).status_code == 201
        # Crontal can't be reached yet: the chat still works, without Crontal tools.
        sent = await client.post("/i/m500/api/chat", headers=free, json={"text": "hi there",
                                                                          "conversation_id": "x"})
        assert sent.status_code == 200, sent.text
        await answered(client, "m500", "42", sent.json()["thread_key"], headers=free)
        first = FakeHarness.runs[-1]
        assert first["model"] == "gpt-6.1-terra"
        assert not any(name.startswith("crontal_get") for name in first["tools"])
        assert any(r.message == "crontal tools not available yet; the tenant works without them"
                   and r.tenant == "m500" for r in caplog.records)
        # Reachable again: the next run picks the tools up after the backoff, no task.
        FakeCrontal.down = set()
        listing = app.state.services.crontal_discovery._listings[CRONTAL_URL]
        listing.retry_at = 0
        sent = await client.post("/i/m500/api/chat", headers=free, json={"text": "again",
                                                                          "conversation_id": "x"})
        await answered(client, "m500", "42", sent.json()["thread_key"], count=2, headers=free)
        assert "crontal_get_rfqs" in FakeHarness.runs[-1]["tools"]
        # 50 inputs, no email, no automations, no console.
        usage = (await client.get("/i/m500/api/limits/inputs", headers=free)).json()
        assert usage["limited"] is True and usage["limit"] == 50 and usage["used"] == 2
        assert (await client.post("/i/m500/ingest/email", content=b"x")).status_code == 403
        assert (await client.get("/i/m500/api/automations", headers=free)).status_code == 403
        assert (await client.get("/i/m500/api/memory", headers=free)).status_code == 403
        assert (await client.post("/i/m500/api/mailboxes", headers=free,
                                  json=MAILBOX)).status_code == 403
        # Legacy chat import (stage 3/4): history only, nothing counted.
        imported = await client.post("/i/m500/api/threads/import",
                                     headers={"Authorization": f"Bearer {key}"}, json={
            "owner_user_id": "42", "conversation_id": "crontal-legacy-9",
            "source": "crontal-legacy", "source_id": "9", "title": "Old quote",
            "created_at": "2026-08-01T09:00:00Z", "messages": [
                {"role": "user", "text": "Need 316L", "created_at": "2026-08-01T09:00:05Z"},
                {"role": "assistant", "text": "Drafted RFQ #12.",
                 "created_at": "2026-08-01T09:00:20Z"}]})
        assert imported.status_code in (200, 201), imported.text
        threads = (await client.get("/i/m500/api/threads", headers=free)).json()
        assert "chat:42:crontal-legacy-9" in {t["key"] for t in threads}
        usage = (await client.get("/i/m500/api/limits/inputs", headers=free)).json()
        assert usage["used"] == 2
        # Its key is the free tenant's alone.
        assert (await client.get("/i/bss/api/me", headers=free)).status_code == 401
        assert (await client.get("/api/me", headers=free)).status_code == 401
        work_on_registry = (await client.get("/i/bss/api/threads",
                                             headers=as_user("bss", "bss-owner"))).json()

    # --- rollback: the registry off ---------------------------------------------
    async with live(env) as (app, client):
        await sees_everything(client, tokens)
        threads = (await client.get("/i/bss/api/threads",
                                    headers=as_user("bss", "bss-owner"))).json()
        assert {t["key"] for t in threads} >= {t["key"] for t in work_on_registry}
        assert (await client.get("/i/m500/api/me", headers={"Authorization": f"Bearer {key}"})
                ).status_code == 404  # unreachable, its data intact
    assert rows(pg_dsn, "SELECT count(*) FROM handler_m500.threads")[0][0] >= 2

    # --- and on again: the free tenant is back ------------------------------------
    async with live(on) as (app, client):
        me = await client.get("/i/m500/api/me", headers=free)
        assert me.status_code == 200 and me.json()["user_id"] == "42"
        await sees_everything(client, tokens)
