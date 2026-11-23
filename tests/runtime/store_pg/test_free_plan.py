"""Free tenants and host hardening (real Postgres, multi-tenant Phase 4)."""
import asyncio
import contextlib
import json
import logging
import os
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
import pytest

from handler.runtime import server
from handler.runtime.context import RunContext
from handler.runtime.gateway.base import ToolSpec
from handler.runtime.gateway.crontal_api import CrontalApiTools
from handler.runtime.gateway.policy import GrantPolicy
from handler.runtime.harness import openai_agents as oa
from handler.runtime.spreadsheets import write_csv
from handler.runtime.storage import (
    StorageLimitReached,
    ensure_workspace_room,
    register_workspace_limit,
    unregister_workspace_limit,
)
from handler.runtime.store.postgres import PostgresStore
from handler.runtime.tenancy import host as host_module
from handler.runtime.tenancy.limits import (
    DEFAULT_PLAN_TABLE,
    LiveLimits,
    parse_plan_table,
    resolve_limits,
)
from handler.runtime.types import Decision, Event, Lane, Risk, Schedule, ThreadSpec, ThreadType
from handler.runtime.uploads import UploadStore
from handler.runtime.web.email_ingest import sign_email
from handler.runtime.web.ingest import sign

from .test_plan_limits import MAIL_SECRETS, post_mail, spooled
from .test_tenant_registry import (  # noqa: F401  (fixtures)
    PLATFORM,
    SPEC,
    FakeHarness,
    activate,
    admin,
    asleep,
    base_env,
    chat,
    create,
    fake_harness,
    live_store,
    raw_email,
    rows,
    serve,
    slot,
    spy,
    state,
    until,
)

AS_PROXY = {"Authorization": "Bearer admin-42", "X-Handler-User": "owner"}


def lifecycle(app, name="m42"):
    context = app.state.services.cache.cached(name)
    return context.lifecycle if context is not None else None


# --- the review's bug: max_runs in the plan table ----------------------------


def test_max_runs_in_the_plan_table_is_refused_clearly():
    with pytest.raises(ValueError, match="HANDLER_PLAN_RUN_CAPS"):
        parse_plan_table('{"free": {"max_runs": 2}}')


def test_a_tenants_own_max_runs_still_resolves(base_env):
    limits = resolve_limits("free", {"max_runs": 2, "monthly_inputs": 9}, parse_plan_table(None))
    assert limits.monthly_inputs == 9


async def test_a_bad_plan_table_stops_the_host_before_any_tenant_starts(base_env):
    env = {**base_env, "HANDLER_PLAN_LIMITS": '{"free": {"max_runs": 2}}'}
    with pytest.raises(ValueError, match="HANDLER_PLAN_RUN_CAPS"):
        server.create_host(server._parse_args(["serve"], env), env)


def test_free_defaults_match_the_owners_decision():
    free = resolve_limits("free", {}, parse_plan_table(None))
    assert (free.model, free.fix_model) == ("gpt-6.1-terra", True)
    assert not (free.email or free.automations or free.connected_mail or free.allow_codex
                or free.console)
    assert free.strict_grants and free.monthly_inputs == 50 and free.at_limit == "drop"
    assert DEFAULT_PLAN_TABLE["paid"] == {}


# --- Codex can't run on free --------------------------------------------------


@pytest.fixture
def codex_spy(monkeypatch):
    built = []

    class Codex(FakeHarness):
        name = "codex"

        def __init__(self, *args, **kwargs):
            built.append(1)

        async def run(self, req):
            raise AssertionError("Codex ran on a free tenant")
            yield  # pragma: no cover

    monkeypatch.setattr(server, "codex_binary", lambda: "/usr/bin/codex")
    monkeypatch.setattr(server, "CodexHarness", Codex)
    return built


async def test_codex_is_never_built_or_used_on_free(base_env, pg_dsn, codex_spy):
    base_env["HANDLER_DEFAULT_HARNESS"] = "codex"  # default harness and settings both say Codex
    async with serve(base_env) as (app, client):
        await create(client, plan="free")
        await activate(client)
        store = live_store(app)
        # A thread created asking for Codex, and a chat override trying to pick it.
        codex_thread = ThreadSpec("chat:u:9", ThreadType.CHAT, Lane.INTERACTIVE,
                                  owner_user_id="u", harness="codex")
        await store.put(Event(type="chat.message", source="t", payload={"text": "via thread"},
                              idempotency_key="t1", thread_key="chat:u:9", user_id="u"), codex_thread)
        await client.post("/i/m42/api/users", headers=admin("admin-42"),
                          json={"id": "u2", "display_name": "U"})
        response = await client.post("/i/m42/api/chat", headers={**admin("admin-42"), "X-Handler-User": "u2"},
                                     json={"text": "via chat", "harness": "codex"})
        assert response.status_code < 300
        # Settings can't be changed to Codex either: there is no console.
        put = await client.put("/i/m42/api/settings/threads", headers=AS_PROXY,
                               json={"chat": {"harness": "codex"}})
        assert put.status_code == 403
        await until(lambda: len(FakeHarness.runs) >= 2)
        threads = rows(pg_dsn, "SELECT key, harness FROM handler_m42.threads ORDER BY key")
    assert codex_spy == []
    assert {harness for _, harness in threads} == {"openai_agents"}


async def test_paid_tenants_keep_codex(base_env, codex_spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
    assert codex_spy == [1]


# --- grants on free -----------------------------------------------------------


@pytest.fixture
def specs(tmp_path):
    tools = CrontalApiTools("http://crontal.test", "token", workspace_for=lambda key: tmp_path)
    found = {spec.action: spec for spec in tools.tools()}
    found["shell.run"] = ToolSpec("shell", "shell.run", Risk.EXEC, "", {"type": "object"},
                                  lambda ctx, args: None)
    found["automation.create"] = ToolSpec("automation_create", "automation.create",
                                          Risk.WRITE_INTERNAL, "", {"type": "object"},
                                          lambda ctx, args: None)
    return found


FREE_LIMITS = LiveLimits(resolve_limits("free", {}, parse_plan_table(None)))
LOOSE = {"actions": {"crontal_api.*": "allow", "automation.*": "allow", "shell.*": "allow"}}
INSIDE = {"method": "POST", "path": "/api/v1/rfq", "json": {"status": "draft"}}
OUTWARD = {"method": "POST", "path": "/api/v1/rfq/7/share"}
TRASH = {"method": "DELETE", "path": "/api/v1/files/3"}


def decide(policy, specs, action, thread, args=None):
    ctx = RunContext(run_id="r", thread_key=f"{thread}:x")
    return policy.decide(ctx, specs[action], args or {})[0]


@pytest.mark.parametrize("action,thread,args,expected", [
    ("crontal_api.get", "chat", {"path": "/api/v1/rfq"}, Decision.ALLOW),
    ("crontal_api.get", "crontal", {"path": "/api/v1/rfq"}, Decision.ALLOW),
    ("crontal_api.call", "chat", INSIDE, Decision.ASK),
    ("crontal_api.call", "chat", OUTWARD, Decision.ASK),
    ("crontal_api.call", "chat", TRASH, Decision.ASK),
    ("crontal_api.call", "crontal", INSIDE, Decision.ASK),
    ("crontal_api.call", "crontal", OUTWARD, Decision.DENY),
    ("crontal_api.call", "crontal", TRASH, Decision.DENY),
    ("shell.run", "chat", {}, Decision.DENY),
    ("automation.create", "chat", {}, Decision.DENY),
])
def test_free_grant_floor_holds_against_loose_settings(specs, action, thread, args, expected):
    assert decide(GrantPolicy(LOOSE, plan_limits=FREE_LIMITS), specs, action, thread, args) == expected


def test_settings_can_still_tighten_the_free_floor(specs):
    policy = GrantPolicy({"actions": {"crontal_api.get": "deny"}}, plan_limits=FREE_LIMITS)
    assert decide(policy, specs, "crontal_api.get", "chat", {"path": "/api/v1/rfq"}) == Decision.DENY


def test_paid_tenants_keep_their_grants(specs):
    paid = LiveLimits(resolve_limits("paid", {}, parse_plan_table(None)))
    policy = GrantPolicy(LOOSE, plan_limits=paid)
    assert decide(policy, specs, "crontal_api.call", "chat", INSIDE) == Decision.ALLOW
    assert decide(policy, specs, "automation.create", "chat") == Decision.ALLOW


# --- no console on free -------------------------------------------------------

CONSOLE = [("GET", "/"), ("GET", "/app"), ("GET", "/app/static/app.js"), ("GET", "/login"),
           ("GET", "/timeline"), ("POST", "/login/ticket"), ("POST", "/api/auth/sign-in-tickets"),
           ("GET", "/api/settings"), ("GET", "/api/settings/prompt"), ("PUT", "/api/settings/persona"),
           ("GET", "/api/logs"), ("GET", "/api/tools"), ("GET", "/api/audit"), ("GET", "/api/memory"),
           ("GET", "/api/usage"), ("GET", "/api/usage/records"), ("GET", "/api/events"),
           ("POST", "/api/users/owner/tokens"), ("GET", "/api/admin/memory-saves"),
           ("GET", "/api/ui/manifest"), ("GET", "/api/stream")]
PANEL = [("GET", "/api/chat/options"), ("GET", "/api/threads"), ("GET", "/api/tasks"),
         ("GET", "/api/approvals"), ("GET", "/api/questions"), ("GET", "/api/notifications"),
         ("GET", "/api/me"), ("GET", "/api/limits/inputs"), ("GET", "/api/uploads")]


async def provision(client, merchant=42, user="owner"):
    """What Crontal does on first use: the user, created with the admin key."""
    response = await client.post(f"/i/m{merchant}/api/users", headers=admin(f"admin-{merchant}"),
                                 json={"id": user, "display_name": user, "role": "admin"})
    assert response.status_code in (201, 409), response.text


async def call(client, method, path, headers):
    return await client.request(method, f"/i/m42{path}", headers=headers,
                                json={} if method in ("POST", "PUT") else None)


@pytest.mark.parametrize("awake", [False, True])
async def test_free_tenants_have_no_admin_console_even_with_the_admin_key(base_env, awake):
    async with serve(base_env) as (app, client):
        await create(client, plan="free")
        await provision(client)
        if awake:
            await activate(client)
        else:
            await asleep(app)
        for method, path in CONSOLE:
            response = await call(client, method, path, AS_PROXY)
            assert response.status_code == 403, (method, path, response.text)
            assert response.json()["feature"] == "console", path
            assert response.json()["error"] == "plan_requires_upgrade"
        for method, path in PANEL:
            response = await call(client, method, path, AS_PROXY)
            assert response.status_code == 200, (method, path, response.text)


async def test_paid_tenants_keep_the_console(base_env):
    async with serve(base_env) as (app, client):
        await create(client)
        assert (await client.get("/i/m42/app")).status_code == 200
        assert (await client.get("/i/m42/api/settings", headers=admin("admin-42"))).status_code == 200


# --- storage limits ------------------------------------------------------------


async def test_uploads_stop_at_the_plan_limit(base_env):
    async with serve(base_env) as (app, client):
        await create(client, plan="free", limits={"max_upload_bytes": 1000})
        await provision(client)
        files = lambda: {"files": ("a.txt", b"x" * 600, "text/plain")}
        first = await client.post("/i/m42/api/uploads", headers=AS_PROXY, files=files())
        assert first.status_code == 200, first.text
        second = await client.post("/i/m42/api/uploads", headers=AS_PROXY, files=files())
        assert second.status_code == 413
        assert second.json()["error"] == "storage_limit_reached"
        assert second.json()["scope"] == "upload" and second.json()["limit_bytes"] == 1000
        assert len((await client.get("/i/m42/api/uploads", headers=AS_PROXY)).json()) == 1


def test_workspace_writes_stop_at_the_limit(tmp_path):
    root = tmp_path / "workspaces"
    (root / "t").mkdir(parents=True)
    register_workspace_limit(root, lambda: 1000)
    try:
        (root / "t" / "big").write_bytes(b"x" * 900)
        ensure_workspace_room(root / "t" / "small", 50)
        with pytest.raises(StorageLimitReached):
            ensure_workspace_room(root / "t" / "more", 200)
        ensure_workspace_room(root / "t" / "big", 950)  # replacing a file counts the difference
        with pytest.raises(StorageLimitReached):
            CrontalApiTools._save(root / "t", "f.pdf", b"x" * 500, "/api/v1/files/1")
        with pytest.raises(ValueError, match="storage"):
            write_csv(root / "t", "out.csv", [{"key": "a", "heading": "A", "type": "text"}],
                      [{"a": "x" * 400}])
        ensure_workspace_room(tmp_path / "elsewhere" / "f", 10_000)  # another tenant: no limit
    finally:
        unregister_workspace_limit(root)
    ensure_workspace_room(root / "t" / "more", 10_000)


async def test_native_write_file_reports_the_limit_to_the_model(tmp_path):
    root = tmp_path / "workspaces"
    workspace = root / "chat-1"
    workspace.mkdir(parents=True)
    register_workspace_limit(root, lambda: 10)
    try:
        from agents.tool_context import ToolContext

        tools = {tool.name: tool for tool in oa.workspace_tools(workspace)}
        arguments = json.dumps({"path": "a.txt", "content": "x" * 100})
        result = await tools["write_file"].on_invoke_tool(
            ToolContext(None, tool_name="write_file", tool_call_id="c1",
                        tool_arguments=arguments), arguments)
    finally:
        unregister_workspace_limit(root)
    assert "storage" in str(result) and not (workspace / "a.txt").exists()


async def test_inbound_mail_is_stored_even_when_the_workspace_is_full(base_env, pg_dsn, monkeypatch):
    from handler.runtime.mail.intake import GateResult
    from handler.runtime.web import email_ingest

    async def accept(*args, **kwargs):
        return GateResult(accepted=True, reason=None, sender="b@example.com", auth=None,
                          trusted=False, scope="b@example.com")

    async def available(*args):
        return True

    monkeypatch.setattr(email_ingest, "check_sender", accept)
    monkeypatch.setattr(email_ingest, "ai_identity_available", available)
    async with serve(base_env) as (app, client):
        await create(client, plan="paid", limits={"max_workspace_bytes": 1}, secrets=MAIL_SECRETS)
        await activate(client)
        response = await post_mail(client, "/i/m42/ingest/email")
        assert response.status_code == 202
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.events")[0][0] == 1


# --- isolation between tenants -------------------------------------------------


@pytest.fixture
async def two(base_env):
    async with serve(base_env) as (app, client):
        await create(client, 1)
        await create(client, 2)
        await activate(client, 1)
        await activate(client, 2)
        yield app, client


async def test_sse_does_not_cross_tenants(two):
    app, client = two
    for path in ("/i/m2/api/stream", "/i/m2/api/threads/chat:u:1/stream", "/i/m2/api/tasks/stream"):
        response = await client.get(path + "?access_token=admin-1")
        assert response.status_code == 401, path


async def test_run_event_bus_does_not_cross_tenants(two):
    from handler.runtime.bus import BusMessage

    app, _ = two
    one, other = lifecycle(app, "m1").bus, lifecycle(app, "m2").bus
    assert one is not other
    async with other.subscribe("*") as queue:
        one.publish(BusMessage("chat:u:1", None, "event_queued"))
        await asyncio.sleep(0.05)
        assert queue.empty()


async def test_mcp_run_tokens_do_not_cross_tenants(two):
    app, client = two
    m1 = app.state.services.cache.cached("m1")
    token = lifecycle(app, "m1").tokens.issue(RunContext(run_id="r", thread_key="chat:u:1"), 60,
                                              gateway=m1.gateway, tenant="m1")
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    assert (await client.post("/i/m2/mcp/", json=body, headers=headers)).status_code == 401
    assert (await client.post("/i/m1/mcp/", json=body, headers=headers)).status_code != 401


async def test_the_host_mcp_route_needs_no_main_tenant(two):
    """Harnesses are given `/mcp/`: on a registry host with no `main`, the run token
    alone still names the tenant (and a disabled or unknown one is refused)."""
    app, client = two
    assert app.state.tenants._slot("main") is None
    m1 = app.state.services.cache.cached("m1")
    token = lifecycle(app, "m1").tokens.issue(RunContext(run_id="r", thread_key="chat:u:1"), 60,
                                              gateway=m1.gateway, tenant="m1")
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    response = await client.post("/mcp/", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert "memory_delete" in {tool["name"] for tool in response.json()["result"]["tools"]}
    assert (await client.post("/mcp/", json=body, headers={
        **headers, "Authorization": "Bearer admin-1"})).status_code == 401


async def test_uploads_do_not_cross_tenants_even_with_hostile_ids(two):
    app, client = two
    await provision(client, 2)
    sent = await client.post("/i/m2/api/uploads", headers={**admin("admin-2"), "X-Handler-User": "owner"},
                             files={"files": ("secret.txt", b"tenant two", "text/plain")})
    file_id = sent.json()[0]["id"]
    for hostile in (file_id, "*", "..", "../m2", "%2e%2e", "[0-9a-f]*", "*" * 32, "?" * 32,
                    file_id[:31] + "*", file_id.upper()):
        response = await client.get(f"/i/m1/api/uploads/{quote(hostile, safe='')}",
                                    headers=admin("admin-1"))
        assert response.status_code == 404, hostile
    store = lifecycle(app, "m1").uploads
    for hostile in ("*" * 32, "?" * 32, "[" + "a" * 30 + "]", f"../{file_id}"):
        assert store.get(hostile) is None


def test_upload_metadata_can_not_point_outside_its_root(tmp_path):
    store = UploadStore(tmp_path / "one")
    meta = store.save("u", "a.txt", "text/plain", [b"x"])
    (tmp_path / "two").mkdir()
    (tmp_path / "two" / "stolen").write_text("other tenant")
    tampered = meta.__class__(**{**meta.__dict__, "user_id": "../../two", "filename": "../stolen"})
    path = store.path(tampered)
    assert path is None or path.is_relative_to((tmp_path / "one").resolve())


async def test_workspaces_do_not_cross_tenants(two, tmp_path):
    app, _ = two
    one, other = lifecycle(app, "m1").workspaces_dir, lifecycle(app, "m2").workspaces_dir
    assert one.resolve() != other.resolve() and not one.resolve().is_relative_to(other.resolve())
    workspace = one / "chat-x"
    workspace.mkdir(parents=True)
    (other / "chat-y").mkdir(parents=True)
    (other / "chat-y" / "secret.txt").write_text("tenant two")
    os.symlink(other / "chat-y", workspace / "link")
    for path in ("../../../", "link/secret.txt", str(other / "chat-y" / "secret.txt")):
        with pytest.raises(ValueError):
            oa._confined(workspace, path)


async def test_outbox_rows_do_not_cross_tenants(two):
    app, _ = two
    one, other = lifecycle(app, "m1").store, lifecycle(app, "m2").store
    await one.enqueue_delivery("crontal", "k1", {"tenant": 1})
    assert await other.claim_deliveries(["crontal"]) == []
    assert [d.key for d in await one.claim_deliveries(["crontal"])] == ["k1"]


def test_file_keys_can_not_escape_a_prefix():
    from handler.runtime.files import clean_key

    for key in ("../m2/x", "a/../../b", "/abs", "a\\b", "a\0b"):
        with pytest.raises(ValueError):
            clean_key(key)


# --- waking a sleeping tenant ----------------------------------------------------


async def test_healthz_never_wakes_a_tenant(base_env, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        for _ in range(5):
            assert (await client.get("/i/m42/healthz")).json() == {"ok": True}
        assert spy.starts.get("m42") is None


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer guessed"},
                                     {"Authorization": "Bearer platform-key"}])
async def test_requests_without_a_valid_credential_do_not_wake_it(base_env, spy, headers):
    async with serve(base_env) as (app, client):
        await create(client)
        for path in ("/i/m42/api/threads", "/i/m42/api/me", "/i/m42/mcp/", "/i/m42/timeline"):
            assert (await client.get(path, headers=headers)).status_code == 401, path
        assert (await client.post("/i/m42/ingest/crontal", content=b"{}",
                                  headers={"X-Crontal-Signature": "sha256=forged"})).status_code == 401
        assert (await client.get("/i/m42/oauth/google/callback?state=forged&code=x")).status_code == 401
        assert spy.starts.get("m42") is None


async def test_public_pages_are_served_without_waking_it(base_env, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        login = await client.get("/i/m42/login")
        assert login.status_code == 200 and "/i/m42" in login.text
        assert (await client.get("/i/m42/app")).status_code == 200
        assert spy.starts.get("m42") is None


async def test_user_tokens_and_sign_in_tickets_wake_it(base_env, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await client.post("/i/m42/api/users", headers=admin("admin-42"),
                          json={"id": "u", "display_name": "U"})
        token = (await client.post("/i/m42/api/users/u/tokens", headers=admin("admin-42"),
                                   json={})).json()["token"]
        ticket = (await client.post("/i/m42/api/auth/sign-in-tickets", headers=admin("admin-42"),
                                    json={"user_id": "u", "display_name": "U"})).json()["ticket"]
        await asleep(app)
        assert (await client.get("/i/m42/api/me", headers=admin(token))).status_code == 200
        assert spy.starts["m42"] == 2
        await asleep(app)
        assert (await client.post("/i/m42/login/ticket", json={"ticket": ticket})).status_code == 200
        assert spy.starts["m42"] == 3


async def test_signed_webhooks_and_mail_wake_it(base_env, spy):
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        body = b'{"id": 1, "recipient_user_id": 1}'
        response = await client.post("/i/m42/ingest/crontal", content=body,
                                     headers={"X-Crontal-Signature": sign("hook-42", body)})
        assert response.status_code < 300 and spy.starts["m42"] == 1
        await until(lambda: FakeHarness.runs
                    and not lifecycle(app).dispatcher.active_count)  # the webhook's run is done
        await asleep(app)
        assert (await post_mail(client, "/i/m42/ingest/email")).status_code < 300
        assert spy.starts["m42"] == 2


async def test_an_anonymous_probe_does_not_build_a_tenant_context(base_env, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await app.state.services.cache.settle()
        builds = spy.starts.get("m42", 0)
        if app.state.services.cache.cached("m42") is not None:
            app.state.services.cache.invalidate("m42")
            await app.state.services.cache.settle()
        assert (await client.get("/i/m42/api/threads")).status_code == 401  # no credential
        assert app.state.services.cache.cached("m42") is None
        assert spy.starts.get("m42", 0) == builds


async def test_unknown_names_cost_no_query(base_env, monkeypatch):
    async with serve(base_env) as (app, client):
        await create(client)
        registry = app.state.registry

        async def refused(*args, **kwargs):
            raise AssertionError("an unknown name reached the database")

        monkeypatch.setattr(registry, "get", refused)
        monkeypatch.setattr(registry, "list", refused)
        for n in range(200):
            assert (await client.get(f"/i/m{n + 1000}/api/me")).status_code == 404


async def test_a_tenant_made_by_another_process_becomes_known(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await asyncio.sleep(0.1)
        registry = app.state.registry
        from handler.runtime.tenancy.registry import Tenant

        sealed = registry.seal({"HANDLER_ADMIN_KEY": "elsewhere"})
        await registry.import_once([Tenant(name="other", schema_name="handler_other",
                                           s3_prefix="handler/other/", secrets=sealed)])
        assert (await client.get("/i/other/healthz")).status_code == 404
        app.state.tenants._names_read_at = 0
        await until(lambda: "other" in app.state.tenants._slots)
        assert (await client.get("/i/other/healthz")).status_code == 200


# --- spool retries are bounded ----------------------------------------------------


async def test_a_spooled_message_stops_retrying_but_is_never_deleted(base_env, pg_dsn, monkeypatch, caplog):
    from handler.runtime.web import email_ingest
    from tests.runtime import reader_fakes

    # Parsing runs in a reader process, so the stand-in must be importable there.
    monkeypatch.setattr(email_ingest, "parse_email_file", reader_fakes.unparseable)
    monkeypatch.setattr(host_module, "SPOOL_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(host_module, "SPOOL_MAX_ATTEMPTS", 3)
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        await client.post("/platform/tenants/42/disable", headers=PLATFORM)
        await post_mail(client, "/i/m42/ingest/email")
        with caplog.at_level(logging.ERROR, logger="handler.runtime.tenancy"):
            await client.post("/platform/tenants/42/enable", headers=PLATFORM)
            await until(lambda: rows(pg_dsn, "SELECT attempts FROM handler_control.spooled_email")[0][0] == 3)
            await asyncio.sleep(0.3)
        attempts, never = rows(pg_dsn, "SELECT attempts, next_attempt_at = 'infinity' "
                                       "FROM handler_control.spooled_email")[0]
        assert (attempts, never) == (3, True)
        assert any("gave up" in record.getMessage() for record in caplog.records)


def test_backoff_grows_and_is_capped():
    base, cap = host_module.SPOOL_RETRY_SECONDS, host_module.SPOOL_RETRY_MAX_SECONDS
    assert base == 300 and cap == 86_400 and host_module.SPOOL_MAX_ATTEMPTS >= 10


# --- no email on free --------------------------------------------------------------


async def test_mail_to_a_free_tenant_is_refused_and_nothing_is_kept(base_env, pg_dsn, spy, monkeypatch):
    async with serve(base_env) as (app, client):
        await create(client, plan="free", secrets=MAIL_SECRETS)
        response = await post_mail(client, "/i/m42/ingest/email")
        assert response.status_code == 403
        assert response.json()["error"] == "plan_requires_upgrade" and response.json()["feature"] == "email"
        await client.post("/platform/tenants/42/disable", headers=PLATFORM)
        assert (await post_mail(client, "/i/m42/ingest/email")).status_code == 403
        await client.post("/platform/tenants/42/enable", headers=PLATFORM)
        spy.fail_next.add("m42")
        assert (await post_mail(client, "/i/m42/ingest/email")).status_code == 403
        assert spooled(pg_dsn) == []
        assert spy.starts.get("m42") is None


async def test_an_upgrade_processes_only_mail_arriving_after_it(base_env, pg_dsn, monkeypatch):
    from handler.runtime.mail.intake import GateResult
    from handler.runtime.web import email_ingest

    async def accept(*args, **kwargs):
        return GateResult(accepted=True, reason=None, sender="b@example.com", auth=None,
                          trusted=False, scope="b@example.com")

    async def available(*args):
        return True

    monkeypatch.setattr(email_ingest, "check_sender", accept)
    monkeypatch.setattr(email_ingest, "ai_identity_available", available)
    async with serve(base_env) as (app, client):
        await create(client, plan="free", secrets=MAIL_SECRETS)
        await post_mail(client, "/i/m42/ingest/email", body=raw_email("Before"))
        await client.put("/platform/tenants/42", headers=PLATFORM, json={"plan": "paid"})
        await asyncio.sleep(0.3)
        response = await post_mail(client, "/i/m42/ingest/email", body=raw_email("After"))
        assert response.status_code == 202
        subjects = [r[0] for r in rows(pg_dsn, "SELECT payload->>'subject' FROM handler_m42.events")]
        assert subjects == ["After"]


async def over_limit_free(client, limit=1):
    await create(client, plan="free", limits={"monthly_inputs": limit}, secrets=MAIL_SECRETS)


async def crontal(client, body):
    raw = json.dumps(body).encode()
    return await client.post("/i/m42/ingest/crontal", content=raw,
                             headers={"X-Crontal-Signature": sign("hook-42", raw)})


NOTE = {"id": 4, "recipient_user_id": 1, "entity_type": "rfq", "entity_id": 3}
MERCHANT = {"id": 9, "scope": "merchant", "recipient_user_id": None, "entity_type": "rfq",
            "entity_id": 5}


@pytest.mark.parametrize("body", [NOTE, MERCHANT], ids=["per-user", "merchant"])
async def test_free_crontal_events_over_the_limit_are_recorded_never_run(base_env, pg_dsn, body):
    async with serve(base_env) as (app, client):
        await over_limit_free(client, limit=0)
        response = await crontal(client, body)
        assert response.status_code == 202
        assert response.json()["over_limit"] is True and "held" not in response.json()
        assert (await crontal(client, body)).json()["duplicate"] is True
        assert rows(pg_dsn, "SELECT status FROM handler_m42.events") == [("over_limit",)]
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.held_inputs")[0][0] == 0
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.threads")[0][0] == 0
        store = live_store(app)
        # Not at the next month's reset...
        store._clock = lambda: datetime.now(timezone.utc).replace(day=1) + timedelta(days=40)
        assert await store.release_held_inputs() == 0
        # ...nor after an upgrade.
        await client.put("/platform/tenants/42", headers=PLATFORM, json={"plan": "paid", "limits": {}})
        await asyncio.sleep(0.3)
        assert rows(pg_dsn, "SELECT status FROM handler_m42.events") == [("over_limit",)]
        assert FakeHarness.runs == []


async def test_free_count_never_passes_the_limit(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await over_limit_free(client, limit=2)
        for n in range(6):
            assert (await crontal(client, {**NOTE, "id": 100 + n})).status_code == 202
        assert rows(pg_dsn, "SELECT sum(used) FROM handler_m42.input_usage")[0][0] == 2
        statuses = sorted(r[0] for r in rows(pg_dsn, "SELECT status FROM handler_m42.events"))
        assert statuses == ["over_limit"] * 4 + ["routed"] * 2


async def test_free_with_email_turned_on_still_holds_nothing(base_env, pg_dsn, monkeypatch):
    from handler.runtime.mail.intake import GateResult
    from handler.runtime.web import email_ingest

    async def accept(*args, **kwargs):
        return GateResult(accepted=True, reason=None, sender="b@example.com", auth=None,
                          trusted=False, scope="b@example.com")

    async def available(*args):
        return True

    monkeypatch.setattr(email_ingest, "check_sender", accept)
    monkeypatch.setattr(email_ingest, "ai_identity_available", available)
    async with serve(base_env) as (app, client):
        await create(client, plan="free", limits={"monthly_inputs": 0, "email": True},
                     secrets=MAIL_SECRETS)
        response = await post_mail(client, "/i/m42/ingest/email")
        assert response.status_code == 202 and response.json()["status"] == "over_limit"
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.held_inputs")[0][0] == 0


async def test_a_plan_set_to_hold_still_holds(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, plan="free", limits={"monthly_inputs": 0, "at_limit": "hold"},
                     secrets=MAIL_SECRETS)
        assert (await crontal(client, NOTE)).json()["held"] is True
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.held_inputs")[0][0] == 1


def test_at_limit_takes_only_hold_or_drop():
    with pytest.raises(ValueError, match="at_limit"):
        parse_plan_table('{"free": {"at_limit": "later"}}')


async def test_platform_refuses_email_intake_on_free(base_env):
    async with serve(base_env) as (app, client):
        response = await client.put("/platform/tenants/8", headers=PLATFORM,
                                    json={"plan": "free", "integrations": ["crontal", "email_intake"]})
        assert response.status_code == 409


# --- no automations on free --------------------------------------------------------


async def test_automations_are_refused_at_every_entry_point_on_free(base_env):
    async with serve(base_env) as (app, client):
        await create(client, plan="free")
        await activate(client)
        for method, path in (("POST", "/api/automations"), ("GET", "/api/automations"),
                             ("POST", "/api/automations/sch_1/run"),
                             ("PATCH", "/api/automations/sch_1"), ("DELETE", "/api/automations/sch_1")):
            response = await call(client, method, path, AS_PROXY)
            assert response.status_code == 403, (method, path)
            assert response.json()["feature"] == "automations"
        gateway = lifecycle(app).gateway
        offered = {spec.name for spec in await gateway.specs_for(RunContext("r", SPEC.key))}
        assert not {name for name in offered if name.startswith("automation_")}
        refused = await gateway.call(RunContext("r", SPEC.key, user_id="u"), "automation_list", {})
        assert refused.is_error and "not included in this plan" in str(refused.content)


async def test_paid_tenants_get_the_automation_tools(base_env):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        gateway = lifecycle(app).gateway
        offered = {spec.name for spec in await gateway.specs_for(RunContext("r", SPEC.key))}
        assert "automation_create" in offered


class ToolViewHarness(FakeHarness):
    """Records the tool view each run gets from the gateway through its own run token,
    and calls automation_create the way a model would."""

    views: list[set[str]] = []
    created: list[bool] = []
    app = None

    async def run(self, req):
        tokens = ToolViewHarness.app.state.services.tokens
        token = req.mcp_servers[0].headers["Authorization"].partition(" ")[2]
        ctx, gateway = tokens.resolve_with_gateway(token)  # the run's own tenant's gateway
        ToolViewHarness.views.append({spec.name for spec in await gateway.specs_for(ctx)})
        result = await gateway.call(ctx, "automation_create", {
            "name": "digest", "prompt": "send the digest", "schedule": "every 1h"})
        ToolViewHarness.created.append(not result.is_error)
        async for event in super().run(req):
            yield event


@contextlib.asynccontextmanager
async def open_stream(app):
    """An SSE connection held open on the tenant (it blocks any restart)."""
    started, gone = asyncio.Event(), asyncio.Event()

    async def receive():
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        started.set()

    scope = {"type": "http", "method": "GET", "path": "/i/m42/api/stream",
             "raw_path": b"/i/m42/api/stream", "root_path": "", "scheme": "http",
             "query_string": b"", "server": ("h", 80), "client": ("c", 1),
             "http_version": "1.1", "headers": [(b"authorization", b"Bearer admin-42")]}
    stream = asyncio.create_task(app.state.tenants(scope, receive, send))
    await asyncio.wait_for(started.wait(), 5)
    try:
        yield
    finally:
        gone.set()
        await asyncio.wait_for(stream, 5)


@pytest.fixture
def tool_view(monkeypatch):
    ToolViewHarness.views, ToolViewHarness.created = [], []
    monkeypatch.setattr(server, "OpenAIAgentsHarness", ToolViewHarness)
    return ToolViewHarness


async def test_an_upgrade_offers_automation_tools_on_the_next_run_without_a_restart(
        base_env, pg_dsn, spy, tool_view):
    """Codex review finding 1: an open SSE stream kept the runtime (and its tools) alive.
    Now the change retires the tenant's context (NOTIFY handler_tenant_changed); the
    stream finishes on the old one, the next run builds from the new plan."""
    async with serve(base_env) as (app, client):
        tool_view.app = app
        await create(client, plan="free")
        await activate(client)
        async with open_stream(app):
            await live_store(app).put(chat("before"), SPEC)
            await until(lambda: tool_view.created)
            assert "automation_create" not in tool_view.views[0] and tool_view.created == [False]
            response = await client.put("/platform/tenants/42", headers=PLATFORM,
                                        json={"plan": "paid"})
            assert response.status_code == 200
            await until(lambda: lifecycle(app) is not None
                        and lifecycle(app).limits.current.automations)
            await live_store(app).put(chat("after"), SPEC)
            await until(lambda: len(tool_view.created) == 2)
            assert spy.starts["m42"] >= 2  # a fresh context, no process restart
            assert "automation_create" in tool_view.views[1] and tool_view.created[1] is True
        assert rows(pg_dsn, "SELECT name FROM handler_m42.schedules") == [("digest",)]


async def test_a_downgrade_removes_automation_tools_on_the_next_run(base_env, spy, tool_view):
    async with serve(base_env) as (app, client):
        tool_view.app = app
        await create(client)
        await activate(client)
        gateway = lifecycle(app).gateway
        async with open_stream(app):
            await live_store(app).put(chat("before"), SPEC)
            await until(lambda: tool_view.created)
            assert "automation_create" in tool_view.views[0] and tool_view.created == [True]
            await client.put("/platform/tenants/42", headers=PLATFORM, json={"plan": "free"})
            await until(lambda: lifecycle(app) is not None
                        and not lifecycle(app).limits.current.automations)
            await live_store(app).put(chat("after"), SPEC)
            await until(lambda: len(tool_view.created) == 2)
            assert spy.starts["m42"] >= 2
            assert not {n for n in tool_view.views[1] if n.startswith("automation_")}
            assert tool_view.created[1] is False  # a call from an older view is refused
            stale = await gateway.call(RunContext("r", SPEC.key, user_id="u"), "automation_create",
                                       {"name": "x", "prompt": "x", "schedule": "every 1h"})
            assert stale.is_error and "automations are not included" in str(stale.content)


async def test_a_downgraded_tenants_schedules_are_kept_paused_and_resume(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        store = live_store(app)
        due = datetime.now(timezone.utc) + timedelta(seconds=1)
        schedule = Schedule(name="daily", prompt="scheduled", spec="every 1h", next_run_at=due,
                            created_by="u", harness="openai_agents")
        await store.create_schedule(schedule)
        await client.put("/platform/tenants/42", headers=PLATFORM, json={"plan": "free"})
        await until(lambda: lifecycle(app) is None
                    or not lifecycle(app).limits.current.automations)
        await asyncio.sleep(1.5)
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.events "
                            "WHERE type = 'automation.fired'")[0][0] == 0
        assert rows(pg_dsn, "SELECT enabled FROM handler_m42.schedules") == [(True,)]
        if lifecycle(app) is not None:
            await until(lambda: not lifecycle(app).dispatcher.active_count)
            await asleep(app)
        await client.put("/platform/tenants/42", headers=PLATFORM, json={"plan": "paid"})
        await until(lambda: rows(pg_dsn, "SELECT count(*) FROM handler_m42.events "
                                         "WHERE type = 'automation.fired'")[0][0] == 1, timeout=20)
