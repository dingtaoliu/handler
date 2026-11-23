"""Registry-hosted tenants and wake on demand (real Postgres, multi-tenant Phase 2)."""
import asyncio
import contextlib
import email.message
import time
from datetime import timedelta

import httpx
import psycopg
import pytest

from handler.runtime import server
from handler.runtime.gateway.secret_box import SecretBox
from handler.runtime.harness.base import RunEvent, RunEventKind
from handler.runtime.store.postgres import PostgresStore
from handler.runtime.tenancy.run_gate import RunGate
from handler.runtime.types import Event, Lane, Schedule, ThreadSpec, ThreadType, TokenUsage, utcnow
from handler.runtime.web.email_ingest import sign_email
from handler.runtime.web.ingest import sign

from .conftest import SERVER_LOG

PLATFORM = {"Authorization": "Bearer platform-key"}


class FakeHarness:
    """Stands in for the native harness; records every run the dispatcher starts."""

    name = "openai_agents"
    runs: list[str] = []
    gate: asyncio.Event | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def run(self, req):
        FakeHarness.runs.append(req.prompt)
        yield RunEvent(RunEventKind.SESSION, {"session_id": "s"})
        if FakeHarness.gate is not None:
            await FakeHarness.gate.wait()
        yield RunEvent(RunEventKind.USAGE, {"model": "fake"}, usage=TokenUsage(input_tokens=1))
        yield RunEvent(RunEventKind.FINAL, {"text": "done", "session_id": "s"})

    async def interrupt(self, run_id):
        pass


@pytest.fixture(autouse=True)
def fake_harness(monkeypatch):
    FakeHarness.runs, FakeHarness.gate = [], None

    def no_codex():
        raise FileNotFoundError("test has no codex")

    monkeypatch.setattr(server, "codex_binary", no_codex)
    monkeypatch.setattr(server, "OpenAIAgentsHarness", FakeHarness)


class RuntimeSpy:
    """Wraps build_tenant_context: counts builds and live contexts per tenant."""

    def __init__(self, monkeypatch):
        self.live: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.starts: dict[str, int] = {}
        self.envs: dict[str, dict] = {}
        self.fail_next: set[str] = set()
        real = server.build_tenant_context

        async def wrapped(args, env, *, instance_name="", **kwargs):
            name = instance_name
            if name in self.fail_next:
                self.fail_next.discard(name)
                raise RuntimeError("context build failed")
            self.envs[name] = dict(env)
            self.starts[name] = self.starts.get(name, 0) + 1
            self.live[name] = self.live.get(name, 0) + 1
            self.peak[name] = max(self.peak.get(name, 0), self.live[name])
            context = await real(args, env, instance_name=name, **kwargs)
            close = context.close

            async def closing():
                try:
                    await close()
                finally:
                    self.live[name] -= 1

            context.close = closing
            return context

        monkeypatch.setattr(server, "build_tenant_context", wrapped)


@pytest.fixture
def spy(monkeypatch):
    return RuntimeSpy(monkeypatch)


@pytest.fixture
def base_env(tmp_path, pg_dsn):
    return {
        "HANDLER_TENANT_REGISTRY": "1",
        "HANDLER_PLATFORM_KEY": "platform-key",
        "HANDLER_TENANT_SECRET_KEY": "sealing-key",
        "DATABASE_URL": pg_dsn,
        "HANDLER_RT_DATA_DIR": str(tmp_path / "data"),
        "OPENAI_API_KEY": "fake-no-network",
        "HANDLER_DEFAULT_HARNESS": "openai_agents",
        "HANDLER_TENANT_SCAN_SECONDS": "0.05",
        "HANDLER_TENANT_IDLE_SECONDS": "3600",
        "HANDLER_TENANT_BOOT_GRACE_SECONDS": "3600",
    }


@contextlib.asynccontextmanager
async def serve(env):
    app = server.create_host(server._parse_args(["serve"], env), env)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://handler") as client:
            yield app, client


async def until(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


def admin(key):
    return {"Authorization": f"Bearer {key}"}


async def create(client, merchant=42, **body):
    body.setdefault("secrets", {"HANDLER_ADMIN_KEY": f"admin-{merchant}"})
    response = await client.put(f"/platform/tenants/{merchant}", headers=PLATFORM, json=body)
    assert response.status_code in (200, 201), response.text
    return response


def rows(dsn, query, *params):
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()


def execute(dsn, query, *params):
    with psycopg.connect(dsn) as conn:
        conn.execute(query, params)


def state(dsn, name="m42"):
    return rows(dsn, "SELECT active_since, next_wake_at FROM handler_control.tenant_state "
                     "WHERE name = %s", name)[0]


def slot(app, name="m42"):
    return app.state.tenants._slots[name]


async def activate(client, merchant=42):
    response = await client.get(f"/i/m{merchant}/api/me", headers=admin(f"admin-{merchant}"))
    assert response.status_code == 200, response.text


def context(app, name="m42"):
    """The tenant's built context (None when not cached)."""
    return app.state.services.cache.cached(name)


async def asleep(app, name="m42"):
    """Evict the tenant's context, as the LRU or TTL would."""
    cache = app.state.services.cache
    assert cache.cached(name) is not None
    cache.invalidate(name)
    await cache.settle()
    assert cache.cached(name) is None


SPEC = ThreadSpec("chat:u:1", ThreadType.CHAT, Lane.INTERACTIVE, owner_user_id="u",
                  harness="openai_agents")


def chat(key, prompt="hello"):
    return Event(type="chat.message", source="test", payload={"text": prompt},
                 idempotency_key=key, thread_key=SPEC.key, user_id="u")


def live_store(app, name="m42"):
    return context(app, name).store


# --- done when: created with no restart ------------------------------------


async def test_tenant_is_created_with_no_restart(base_env, pg_dsn, spy):
    async with serve(base_env) as (app, client):
        assert (await client.get("/i/m42/healthz")).status_code == 404
        response = await create(client)
        assert response.status_code == 201
        body = response.json()
        assert (body["name"], body["schema"], body["s3_prefix"], body["base_path"]) == (
            "m42", "handler_m42", "handler/m42/", "/i/m42")
        assert "secrets" not in body
        await activate(client)
        assert spy.starts["m42"] >= 1  # a context, built on demand
        assert rows(pg_dsn, "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema = 'handler_m42' AND table_name = 'threads'")[0][0] == 1


# --- done when: idle tenants issue no queries ------------------------------


async def test_idle_tenant_issues_no_queries(base_env, pg_dsn):
    """Reads Postgres's own statement log, so it can't miss a query from any code path."""
    base_env["HANDLER_CLAIM_POLL_SECONDS"] = "0.05"
    base_env["HANDLER_OUTBOX_POLL_SECONDS"] = "0.05"
    dbname = pg_dsn.rsplit("dbname=", 1)[1]
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f'ALTER DATABASE "{dbname}" SET log_statement = \'all\'')
    log = SERVER_LOG["path"]

    def since(offset):
        with open(log) as handle:
            handle.seek(offset)
            return handle.read()

    async with serve(base_env) as (app, client):
        await create(client)
        offset = log.stat().st_size
        await activate(client)
        await until(lambda: "handler_m42" in since(offset))  # the detector sees a busy tenant
        await settled(pg_dsn)
        await asyncio.sleep(0.3)
        offset = log.stat().st_size
        await asyncio.sleep(1.0)  # about 20 passes of each host loop
        seen = since(offset)
    assert "tenant_state" in seen  # the host's loops really ran
    for marker in ("handler_m42", "search_path TO", "FROM threads", "FROM events", "FROM schedules",
                   "FROM deliveries", "FROM settings"):
        assert marker not in seen, marker


async def settled(dsn, name="m42", timeout=10.0):
    """The host loops have looked at the tenant and rescheduled it (nothing due)."""
    await until(lambda: rows(dsn, "SELECT next_wake_at IS NULL AND next_delivery_at IS NULL "
                                  "FROM handler_control.tenant_state WHERE name = %s",
                             name)[0][0], timeout)


async def test_an_unused_context_is_closed_after_the_ttl(base_env, spy):
    base_env["HANDLER_TENANT_CACHE_TTL_SECONDS"] = "0.2"
    base_env["HANDLER_CLAIM_POLL_SECONDS"] = "0.05"
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await until(lambda: context(app) is None)  # expired by the claim loop's pass
        await until(lambda: spy.live["m42"] == 0)  # and closed: nothing of it remains
        builds = spy.starts["m42"]
        await activate(client)  # built again on the next request
        assert spy.starts["m42"] == builds + 1


async def test_the_cache_keeps_at_most_its_size(base_env, spy):
    base_env["HANDLER_TENANT_CACHE_SIZE"] = "2"
    async with serve(base_env) as (app, client):
        for merchant in (1, 2, 3):
            await create(client, merchant)
            await activate(client, merchant)
        await app.state.services.cache.settle()
        assert len(app.state.services.cache) <= 2
        assert context(app, "m1") is None  # least recently used went first
        assert sum(spy.live.values()) <= 2


# --- done when: crash recovery ---------------------------------------------


async def test_shutdown_with_work_pending_resumes_after_restart(base_env, pg_dsn):
    FakeHarness.gate = asyncio.Event()
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        store = live_store(app)
        await store.put(chat("c1", "first"), SPEC)
        await until(lambda: FakeHarness.runs)
        await store.put(chat("c2", "second"), SPEC)
        FakeHarness.gate.set()
    FakeHarness.gate, FakeHarness.runs = None, []
    async with serve(base_env) as (app, client):
        await until(lambda: any("second" in run for run in FakeHarness.runs)
                    or rows(pg_dsn, "SELECT count(*) FROM handler_m42.threads "
                                    "WHERE pending_since IS NOT NULL")[0][0] == 0)
    assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.threads "
                        "WHERE pending_since IS NOT NULL")[0][0] == 0


async def test_a_run_killed_mid_way_is_reclaimed_when_its_lease_expires(base_env, pg_dsn):
    """A crash: a worker claimed the thread, started its run and died. Nothing completes
    or fails it; the claim loop records the lease's end as the tenant's next look, and
    when the lease expires it claims the thread again, sweeping the dead run."""
    from handler.runtime.types import Run, RunStatus

    base_env["HANDLER_CLAIM_POLL_SECONDS"] = "0.05"
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        gate: RunGate = app.state.run_gate
        gate.configure("other", "paid", gate.max_runs)
        held = gate.reserve("other", gate.max_runs)  # every slot taken: the loop waits
        store = live_store(app)
        await store.put(chat("crash", "crash work"), SPEC)
        lease, = await store.claim("dead-worker", Lane.INTERACTIVE, 1, 1.0)
        assert await store.create_run(lease, Run("run-dead", SPEC.key, lease.version, [],
                                                 RunStatus.RUNNING, "openai_agents", None))
        gate.release("other", held)  # the dead worker's process is gone
        def rescheduled():
            wake = rows(pg_dsn, "SELECT next_wake_at FROM handler_control.tenant_state "
                                "WHERE name = 'm42'")[0][0]
            return wake is not None and wake > lease.until - timedelta(seconds=0.5)

        await until(rescheduled)
        wake, = rows(pg_dsn, "SELECT next_wake_at FROM handler_control.tenant_state "
                             "WHERE name = 'm42'")[0]
        assert abs((wake - lease.until).total_seconds()) < 1  # the lease's end, not "never"
        await until(lambda: any("crash work" in run for run in FakeHarness.runs))
        await until(lambda: rows(pg_dsn, "SELECT count(*) FROM handler_m42.runs "
                                         "WHERE status = 'completed'")[0][0] == 1)
        statuses = dict(rows(pg_dsn, "SELECT id, status FROM handler_m42.runs"))
    assert statuses["run-dead"] == "abandoned"


# --- done when: scheduled wake-ups -----------------------------------------


async def test_schedule_fires_for_a_tenant_with_no_context(base_env, pg_dsn, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        due = utcnow() + timedelta(seconds=1)
        await live_store(app).create_schedule(Schedule(
            name="s", prompt="scheduled work", spec="every 1h", next_run_at=due,
            harness="openai_agents"))
        await asleep(app)
        await until(lambda: any("scheduled work" in run for run in FakeHarness.runs))


async def test_outbox_retry_is_delivered_by_the_host_outbox_loop(base_env, pg_dsn):
    base_env["HANDLER_OUTBOX_POLL_SECONDS"] = "0.05"
    delivered = []

    class Sink:
        name = "test.sink"
        from handler.runtime.sinks import RetryPolicy
        retry = RetryPolicy()

        def accepts(self, event):
            return True

        async def deliver(self, payload):
            delivered.append(payload["key"])

    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        context(app).outbox.register(Sink())
        store = live_store(app)
        soon = utcnow() + timedelta(milliseconds=300)
        await store.enqueue_delivery("test.sink", "k1", {"key": "k1"})
        await until(lambda: delivered == ["k1"])
        await store.enqueue_delivery("test.sink", "k2", {"key": "k2"})
        await store.delivery_failed("test.sink", "k2", "down", 0.3)
        assert rows(pg_dsn, "SELECT next_delivery_at FROM handler_control.tenant_state "
                            "WHERE name = 'm42'")[0][0] is not None
        await until(lambda: delivered == ["k1", "k2"])
        assert utcnow() >= soon


# --- lost wake-ups ---------------------------------------------------------


async def test_work_from_another_process_runs(base_env, pg_dsn, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await asleep(app)
        cli = PostgresStore(pg_dsn, schema="handler_m42")
        try:
            await cli.put(chat("cli", "from a cli"), SPEC)
        finally:
            await cli.close()
        await until(lambda: any("from a cli" in run for run in FakeHarness.runs))


async def test_work_missed_while_the_listener_is_down_is_found_by_the_poll(base_env, pg_dsn):
    base_env["HANDLER_CLAIM_POLL_SECONDS"] = "0.1"
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await asleep(app)
        hub = app.state.registry.hub
        hub._listener.cancel()  # nothing hears notifications now
        await asyncio.gather(hub._listener, return_exceptions=True)
        cli = PostgresStore(pg_dsn, schema="handler_m42")
        try:
            await cli.put(chat("missed", "missed notify"), SPEC)
        finally:
            await cli.close()
        # The writer marked tenant_state in its transaction; the poll reads it.
        await until(lambda: any("missed notify" in run for run in FakeHarness.runs))


async def test_listener_reconnect_looks_at_every_tenant(base_env, pg_dsn):
    base_env["HANDLER_CLAIM_POLL_SECONDS"] = "3600"  # only a signal can prompt a pass
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await asleep(app)
        await asyncio.sleep(0.1)
        old = PostgresStore(pg_dsn, schema="handler_m42")
        await old.get_thread("x")
        old._marks_wake = None  # an older image: it marks nothing
        rows(pg_dsn, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = current_database() AND query ILIKE 'LISTEN%%'")
        try:
            await old.put(chat("gap", "written in the gap"), SPEC)
        finally:
            await old.close()
        await until(lambda: any("written in the gap" in run for run in FakeHarness.runs))


async def test_work_with_no_signal_at_all_is_found_at_boot(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await asleep(app)
    # Written by a process that neither marks a wake nor is heard: an older image.
    cli = PostgresStore(pg_dsn, schema="handler_m42")
    try:
        await cli.get_thread("x")
        cli._marks_wake = None
        await cli.put(chat("silent", "silent work"), SPEC)
    finally:
        await cli.close()
    execute(pg_dsn, "UPDATE handler_control.tenant_state SET next_wake_at = NULL")
    async with serve(base_env) as (app, client):
        await until(lambda: any("silent work" in run for run in FakeHarness.runs))


async def test_a_failed_build_is_retried_without_a_request(base_env, pg_dsn, spy):
    base_env["HANDLER_CLAIM_POLL_SECONDS"] = "0.1"
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await asleep(app)
        spy.fail_next.add("m42")
        response = await client.get("/i/m42/api/me", headers=admin("admin-42"))
        assert response.status_code == 503  # the sender retries; nothing was accepted and lost
        assert response.headers["retry-after"] == "5"
        cli = PostgresStore(pg_dsn, schema="handler_m42")
        try:
            await cli.put(chat("later", "after a failed build"), SPEC)
        finally:
            await cli.close()
        spy.fail_next.add("m42")  # the claim loop's first build fails too; it stays due
        await until(lambda: any("after a failed build" in run for run in FakeHarness.runs))


async def test_crontal_notification_on_an_unbuilt_tenant_runs(base_env, pg_dsn, spy):
    secrets = {"HANDLER_ADMIN_KEY": "admin-42", "HANDLER_WEBHOOK_SECRET": "hook-42"}
    async with serve(base_env) as (app, client):
        await create(client, secrets=secrets)
        await activate(client)
        await asleep(app)
        body = b'{"id": 7, "recipient_user_id": 1, "type": "rfq", "entity_type": "rfq", "entity_id": 3}'
        response = await client.post("/i/m42/ingest/crontal", content=body,
                                     headers={"X-Crontal-Signature": sign("hook-42", body)})
        assert response.status_code < 300, response.text
        await until(lambda: FakeHarness.runs)


def raw_email(subject="Quote please"):
    message = email.message.EmailMessage()
    message["From"], message["To"] = "buyer@example.com", "contact@crontal.ai"
    message["Subject"], message["Message-ID"] = subject, "<one@example.com>"
    message.set_content("Please quote 10 t of 316L.")
    return message.as_bytes()


async def post_email(client, name, secret, body):
    timestamp, signature = sign_email(secret, body)
    return await client.post(f"/i/{name}/ingest/email", content=body, headers={
        "x-handler-timestamp": timestamp, "x-handler-signature": signature})


async def test_inbound_email_to_an_unbuilt_tenant_is_stored(base_env, pg_dsn, spy):
    secrets = {"HANDLER_ADMIN_KEY": "admin-42", "HANDLER_EMAIL_SECRET": "mail-42"}
    async with serve(base_env) as (app, client):
        await create(client, secrets=secrets)
        await activate(client)
        await asleep(app)
        response = await post_email(client, "m42", "mail-42", raw_email())
        assert response.status_code < 300, response.text
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.events")[0][0] == 1


async def test_a_context_in_use_is_not_closed_by_invalidation(base_env, pg_dsn, spy):
    """A change mid-request retires the context; the request finishes on it, the next
    one gets a fresh build, and the old one closes once its last user leaves."""
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        cache = app.state.services.cache
        async with cache.use("m42") as held:
            cache.invalidate("m42")
            await cache.settle()
            assert spy.live["m42"] == 1  # still held: not closed
            await activate(client)  # a new request builds afresh
            assert context(app) is not held and spy.live["m42"] == 2
        await cache.settle()
        assert spy.live["m42"] == 1 and spy.peak["m42"] == 2


# --- mailboxes poll from the host timer; nothing pins a tenant --------------


async def add_mailbox(app, name="m42", *, enabled=True):
    from handler.runtime.integrations.mailbox.config import Mailbox
    from handler.runtime.integrations.mailbox.store import MailStore

    # Loopback port 1: the poller's connection is refused at once, with no network.
    mailbox = Mailbox(email="sales@example.com", username="sales@example.com", label="Sales",
                      imap_host="127.0.0.1", imap_port=1, imap_security="tls",
                      smtp_host="127.0.0.1", smtp_port=1, smtp_security="tls", enabled=enabled)
    await MailStore(live_store(app, name)).put_mailbox(mailbox)
    return mailbox


async def test_a_connected_mailbox_does_not_keep_a_context(base_env, spy):
    base_env["HANDLER_TENANT_CACHE_TTL_SECONDS"] = "0.2"
    base_env["HANDLER_CLAIM_POLL_SECONDS"] = "0.05"
    async with serve(base_env) as (app, client):
        await create(client, integrations=["mailbox"])
        await activate(client)
        await add_mailbox(app)
        await until(lambda: spy.live["m42"] == 0, timeout=15)  # polls are due rows now


# --- platform API and isolation --------------------------------------------


async def test_create_is_idempotent_and_returns_a_generated_key_once(base_env):
    async with serve(base_env) as (app, client):
        first = await client.put("/platform/tenants/7", headers=PLATFORM, json={})
        assert first.status_code == 201 and first.json()["admin_key"]
        second = await client.put("/platform/tenants/7", headers=PLATFORM, json={})
        assert second.status_code == 200 and "admin_key" not in second.json()
        response = await client.get("/i/m7/api/me", headers=admin(first.json()["admin_key"]))
        assert response.status_code == 200


async def test_update_merges_and_a_restart_applies_new_secrets(base_env, spy):
    async with serve(base_env) as (app, client):
        await create(client, config={"HANDLER_RT_MODEL": "one"})
        await activate(client)
        response = await client.put("/platform/tenants/42", headers=PLATFORM, json={
            "plan": "free", "secrets": {"HANDLER_ADMIN_KEY": "rotated"}})
        assert response.status_code == 200
        assert response.json()["plan"] == "free"
        assert response.json()["config"] == {"HANDLER_RT_MODEL": "one"}
        await until(lambda: spy.starts["m42"] >= 2)  # the change retired its context
        assert (await client.get("/i/m42/api/me", headers=admin("rotated"))).status_code == 200
        assert (await client.get("/i/m42/api/me", headers=admin("admin-42"))).status_code == 401
        assert spy.envs["m42"]["HANDLER_RT_MODEL"] == "one"


async def test_disable_stops_the_tenant_and_enable_resumes_its_work(base_env, pg_dsn, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        response = await client.post("/platform/tenants/42/disable", headers=PLATFORM)
        assert response.status_code == 200 and response.json()["enabled"] is False
        assert context(app) is None  # its context retired; the claim loop skips it
        assert not app.state.services.tenants.get("m42").enabled
        assert (await client.post("/platform/tenants/42/disable", headers=PLATFORM)).status_code == 200
        assert (await client.get("/i/m42/healthz")).status_code == 404
        cli = PostgresStore(pg_dsn, schema="handler_m42")
        try:
            await cli.put(chat("while-off", "while off"), SPEC)
        finally:
            await cli.close()
        await asyncio.sleep(0.2)
        assert not FakeHarness.runs  # held, not lost
        assert (await client.post("/platform/tenants/42/enable", headers=PLATFORM)).status_code == 200
        await until(lambda: any("while off" in run for run in FakeHarness.runs))


async def test_platform_key_and_tenant_admin_keys_do_not_cross(base_env):
    async with serve(base_env) as (app, client):
        await create(client)
        assert (await client.get("/platform/tenants", headers=admin("admin-42"))).status_code == 401
        assert (await client.get("/platform/tenants")).status_code == 401
        assert (await client.get("/i/m42/api/me", headers=PLATFORM)).status_code == 401
        response = await client.get("/platform/tenants", headers=PLATFORM)
        assert response.status_code == 200 and "secrets" not in response.json()[0]


async def test_platform_key_cannot_be_used_as_an_admin_key(base_env):
    async with serve(base_env) as (app, client):
        response = await client.put("/platform/tenants/5", headers=PLATFORM, json={
            "secrets": {"HANDLER_ADMIN_KEY": "platform-key"}})
        assert response.status_code == 409


async def test_a_tenants_environment_holds_only_its_own_secrets(base_env, spy):
    base_env["GOOGLE_OAUTH_CLIENT_SECRET"] = "process-google"
    async with serve(base_env) as (app, client):
        await create(client, 1, secrets={"HANDLER_ADMIN_KEY": "a1", "HANDLER_WEBHOOK_SECRET": "hook-1",
                                         "CRONTAL_AGENT_TOKEN": "token-1"})
        await create(client, 2, secrets={"HANDLER_ADMIN_KEY": "a2", "HANDLER_SECRET_KEY": "enc-2"})
        await activate_with(client, "m1", "a1")
        await activate_with(client, "m2", "a2")
    first, second = spy.envs["m1"], spy.envs["m2"]
    assert first["HANDLER_WEBHOOK_SECRET"] == "hook-1" and second["HANDLER_SECRET_KEY"] == "enc-2"
    assert not {"HANDLER_WEBHOOK_SECRET", "CRONTAL_AGENT_TOKEN"} & set(second)
    assert "HANDLER_SECRET_KEY" not in first
    for env in (first, second):
        for key in ("HANDLER_PLATFORM_KEY", "HANDLER_TENANT_SECRET_KEY", "HANDLER_TENANT_REGISTRY",
                    "GOOGLE_OAUTH_CLIENT_SECRET"):
            assert key not in env
    joined = " ".join(second.values())
    for secret in ("hook-1", "token-1", "a1", "platform-key", "sealing-key"):
        assert secret not in joined.split()


async def activate_with(client, name, key):
    assert (await client.get(f"/i/{name}/api/me", headers=admin(key))).status_code == 200


async def test_secrets_are_encrypted_at_rest(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, secrets={"HANDLER_ADMIN_KEY": "very-secret-admin"})
    stored = rows(pg_dsn, "SELECT secrets::text, config::text FROM handler_control.tenants")[0]
    assert "very-secret-admin" not in stored[0] + stored[1]
    assert "very-secret-admin" in SecretBox("sealing-key").decrypt(stored[0])


@pytest.mark.parametrize("key", ["HANDLER_ADMIN_KEY", "HANDLER_WEBHOOK_SECRET", "HANDLER_EMAIL_SECRET",
                                 "HANDLER_SECRET_KEY"])
async def test_reused_tenant_credentials_are_rejected(base_env, key):
    async with serve(base_env) as (app, client):
        await create(client, 1, secrets={"HANDLER_ADMIN_KEY": "a1", key: "shared"}
                     if key != "HANDLER_ADMIN_KEY" else {key: "shared"})
        response = await client.put("/platform/tenants/2", headers=PLATFORM, json={
            "secrets": {"HANDLER_ADMIN_KEY": "shared" if key == "HANDLER_ADMIN_KEY" else "a2",
                        key: "shared"}})
        assert response.status_code == 409, response.text


async def test_overlapping_s3_prefixes_and_data_dirs_are_rejected(base_env):
    base_env["HANDLER_FILE_STORE"] = "s3"
    base_env["BUCKETEER_BUCKET_NAME"] = "b"
    async with serve(base_env) as (app, client):
        registry = app.state.registry
        await create(client, 1)
        tenants = await registry.list()
        from dataclasses import replace

        with pytest.raises(ValueError, match="S3 prefixes"):
            registry.validate([*tenants, replace(
                tenants[0], name="x", merchant_id=None, schema_name="handler_x",
                s3_prefix="handler/m1/sub/", secrets=registry.seal({"HANDLER_ADMIN_KEY": "other"}))])
        with pytest.raises(ValueError, match="data directories"):
            registry.validate([*tenants, replace(
                tenants[0], name="x", merchant_id=None, schema_name="handler_x", s3_prefix="other/",
                config={"HANDLER_RT_DATA_DIR": base_env["HANDLER_RT_DATA_DIR"] + "/instances/m1"},
                secrets=registry.seal({"HANDLER_ADMIN_KEY": "other"}))])
        with pytest.raises(ValueError, match="schemas"):
            registry.validate([*tenants, replace(
                tenants[0], name="x", merchant_id=None, s3_prefix="other/",
                secrets=registry.seal({"HANDLER_ADMIN_KEY": "other"}))])


@pytest.mark.parametrize("body,status", [
    ({"secrets": {"DATABASE_URL": "x"}}, 422), ({"config": {"HANDLER_DB_SCHEMA": "x"}}, 422),
    ({"config": {"HANDLER_TRUSTED_USER": "x"}}, 422), ({"unknown": 1}, 422),
])
async def test_platform_api_only_accepts_known_settings(base_env, body, status):
    async with serve(base_env) as (app, client):
        assert (await client.put("/platform/tenants/9", headers=PLATFORM, json=body)).status_code == status


async def test_unknown_names_are_404(base_env):
    async with serve(base_env) as (app, client):
        await create(client)
        for path in ("/i/nope/healthz", "/i/m42", "/i/", "/i/BAD/healthz", "/healthz"):
            assert (await client.get(path)).status_code == 404, path


# --- import and rollback ---------------------------------------------------


def named_env(base_env):
    return {**base_env, "HANDLER_INSTANCES": "main,bss,yeup", "HANDLER_DB_SCHEMA": "handler_crontal",
            "HANDLER_FILE_PREFIX": "handler/crontal/", "HANDLER_ADMIN_KEY": "main-admin",
            "HANDLER_WEBHOOK_SECRET": "main-hook", "HANDLER_I_BSS__HANDLER_ADMIN_KEY": "bss-admin",
            "HANDLER_I_BSS__HANDLER_MERCHANT_ID": "1", "HANDLER_I_YEUP__HANDLER_ADMIN_KEY": "yeup-admin",
            "HANDLER_I_BSS__HANDLER_RT_MODEL": "bss-model",
            "HANDLER_I_YEUP__HANDLER_INTEGRATIONS": "crontal"}


async def test_env_instances_are_imported_once_and_keep_everything(base_env, pg_dsn, spy):
    env = named_env(base_env)
    seeded = PostgresStore(pg_dsn, schema="handler_bss")
    await seeded.put(chat("before"), SPEC)
    await seeded.close()
    async with serve(env) as (app, client):
        listed = {t["name"]: t for t in (await client.get("/platform/tenants", headers=PLATFORM)).json()}
        assert {name: (t["schema"], t["s3_prefix"]) for name, t in listed.items()} == {
            "main": ("handler_crontal", "handler/crontal/"), "bss": ("handler_bss", "handler/bss/"),
            "yeup": ("handler_yeup", "handler/yeup/")}
        assert listed["bss"]["merchant_id"] == 1 and listed["bss"]["config"]["HANDLER_RT_MODEL"] == "bss-model"
        assert listed["yeup"]["integrations"] == ["crontal"]
        assert (await client.get("/healthz")).status_code == 200  # main keeps its root alias
        assert (await client.get("/api/me", headers=admin("main-admin"))).status_code == 200
        assert (await client.get("/i/main/healthz")).status_code == 200
        assert (await client.get("/i/bss/api/me", headers=admin("main-admin"))).status_code == 401
        threads = await client.get("/i/bss/api/threads", headers=admin("bss-admin"))
        assert [t["key"] for t in threads.json()] == [SPEC.key]
        await until(lambda: FakeHarness.runs)
    assert spy.envs["main"]["HANDLER_WEBHOOK_SECRET"] == "main-hook"
    assert "HANDLER_ADMIN_KEY" in spy.envs["bss"] and spy.envs["bss"]["HANDLER_ADMIN_KEY"] == "bss-admin"
    env["HANDLER_I_BSS__HANDLER_RT_MODEL"] = "ignored-after-import"
    env["HANDLER_INSTANCES"] = "main,bss,yeup,extra"
    env["HANDLER_I_EXTRA__HANDLER_ADMIN_KEY"] = "extra-admin"
    async with serve(env) as (app, client):
        listed = {t["name"]: t for t in (await client.get("/platform/tenants", headers=PLATFORM)).json()}
        assert set(listed) == {"main", "bss", "yeup"}
        assert listed["bss"]["config"]["HANDLER_RT_MODEL"] == "bss-model"


async def test_registry_off_leaves_env_hosting_untouched(base_env, pg_dsn, spy):
    env = named_env(base_env)
    env.pop("HANDLER_TENANT_REGISTRY")
    app = server.create_host(server._parse_args(["serve"], env), env)
    assert not hasattr(app.state, "tenants")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://h") as client:
            assert (await client.get("/i/bss/healthz")).status_code == 200
            assert (await client.get("/platform/tenants", headers=PLATFORM)).status_code != 200
    tables = {r[0] for r in rows(pg_dsn, "SELECT table_name FROM information_schema.tables "
                                         "WHERE table_schema = 'handler_control'")}
    assert tables == {"tenant_schema_versions", "due_work", "tenant_state"}


async def test_registry_needs_its_keys_and_database(base_env, tmp_path):
    for missing in ("HANDLER_PLATFORM_KEY", "HANDLER_TENANT_SECRET_KEY", "DATABASE_URL"):
        env = {k: v for k, v in base_env.items() if k != missing}
        with pytest.raises(ValueError):
            server.create_host(server._parse_args(["serve"], env), env)


async def test_env_hosting_works_after_the_registry_has_run(base_env, pg_dsn, spy):
    env = named_env(base_env)
    async with serve(env) as (app, client):
        await until(lambda: "main" in app.state.tenants._slots)
        assert (await client.get("/i/bss/healthz")).status_code == 200
    env.pop("HANDLER_TENANT_REGISTRY")
    app = server.create_host(server._parse_args(["serve"], env), env)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://h") as client:
            assert (await client.get("/i/bss/api/me", headers=admin("bss-admin"))).status_code == 200


async def test_stores_without_a_registry_do_not_mark_wakes(pg_dsn):
    store = PostgresStore(pg_dsn, schema="handler_solo")
    try:
        await store.get_thread("x")
        assert not store._marks_wake
    finally:
        await store.close()


# --- fair scheduling on real dispatchers -----------------------------------


async def test_a_busy_tenant_cannot_starve_another(base_env, pg_dsn):
    base_env["HANDLER_GLOBAL_MAX_RUNS"] = "2"
    base_env["HANDLER_PLAN_RUN_CAPS"] = "free:1,paid:2"
    FakeHarness.gate = asyncio.Event()
    async with serve(base_env) as (app, client):
        await create(client, 1, plan="paid")
        await create(client, 2, plan="paid")
        await activate(client, 1)
        await activate(client, 2)
        gate: RunGate = app.state.run_gate
        busy, quiet = live_store(app, "m1"), live_store(app, "m2")
        for n in range(4):
            key = f"chat:u:busy{n}"
            spec = ThreadSpec(key, ThreadType.CHAT, Lane.INTERACTIVE, owner_user_id="u",
                              harness="openai_agents")
            await busy.put(Event(type="chat.message", source="t", payload={"text": f"busy {n}"},
                                 idempotency_key=key, thread_key=key, user_id="u"), spec)
        await until(lambda: gate.running("m1") == 2)
        await quiet.put(chat("q", "quiet work"), SPEC)
        await until(lambda: gate.waiting("m2"))
        FakeHarness.gate.set()
        FakeHarness.gate = None
        await until(lambda: any("quiet work" in run for run in FakeHarness.runs))
        await until(lambda: gate.total == 0)
        assert sum("busy" in run for run in FakeHarness.runs) == 4


# --- native-only host (HANDLER_ALLOWED_HARNESSES) ---------------------------


async def test_native_only_host_rejects_a_codex_tenant(base_env, spy):
    base_env["HANDLER_ALLOWED_HARNESSES"] = "openai_agents"
    async with serve(base_env) as (app, client):
        response = await client.put("/platform/tenants/42", headers=PLATFORM, json={
            "secrets": {"HANDLER_ADMIN_KEY": "admin-42"},
            "config": {"HANDLER_DEFAULT_HARNESS": "codex"}})
        assert response.status_code == 422 and "not allowed" in response.text
        response = await client.put("/platform/tenants/42", headers=PLATFORM, json={
            "config": {"HANDLER_DEFAULT_HARNESS": "nonsense"}})
        assert response.status_code == 422
        assert await app.state.registry.list() == []
        await create(client, config={"HANDLER_DEFAULT_HARNESS": "openai_agents"})
        response = await client.put("/platform/tenants/42", headers=PLATFORM, json={
            "config": {"HANDLER_DEFAULT_HARNESS": "codex"}})
        assert response.status_code == 422
        tenant, = await app.state.registry.list()
        assert tenant.config == {"HANDLER_DEFAULT_HARNESS": "openai_agents"}
        from dataclasses import replace

        with pytest.raises(ValueError, match="not allowed"):  # e.g. a row written by hand
            app.state.registry.validate([replace(
                tenant, config={"HANDLER_DEFAULT_HARNESS": "codex"})])
        # The tenant's own settings cannot choose Codex either, and it never sees the list.
        await activate(client)
        response = await client.put("/i/m42/api/settings/threads", headers=admin("admin-42"),
                                    json={"chat": {"harness": "codex"}})
        assert response.status_code == 422
        assert "HANDLER_ALLOWED_HARNESSES" not in spy.envs["m42"]
