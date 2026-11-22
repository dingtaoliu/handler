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
from handler.runtime.store.postgres_hub import PostgresHub
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
    """Wraps instance_runtime: counts live runtimes per tenant and can hold a stop open."""

    def __init__(self, monkeypatch):
        self.live: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.starts: dict[str, int] = {}
        self.envs: dict[str, dict] = {}
        self.hold_stop: dict[str, asyncio.Event] = {}
        self.stopping: dict[str, asyncio.Event] = {}
        self.fail_next: set[str] = set()
        real = server.instance_runtime

        @contextlib.asynccontextmanager
        async def wrapped(args, env, *, instance_name="", **kwargs):
            name = instance_name
            if name in self.fail_next:
                self.fail_next.discard(name)
                raise RuntimeError("activation failed")
            self.envs[name] = dict(env)
            self.starts[name] = self.starts.get(name, 0) + 1
            self.live[name] = self.live.get(name, 0) + 1
            self.peak[name] = max(self.peak.get(name, 0), self.live[name])
            try:
                async with real(args, env, instance_name=name, **kwargs) as app:
                    yield app
                if name in self.hold_stop:  # stopped and detached, the lock still held
                    self.stopping.setdefault(name, asyncio.Event()).set()
                    with contextlib.suppress(TimeoutError):  # a failing test must still exit
                        await asyncio.wait_for(self.hold_stop[name].wait(), 15)
            finally:
                self.live[name] -= 1

        monkeypatch.setattr(server, "instance_runtime", wrapped)


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


def state(dsn, name="m42"):
    return rows(dsn, "SELECT active_since, next_wake_at FROM handler_control.tenant_state "
                     "WHERE name = %s", name)[0]


def slot(app, name="m42"):
    return app.state.tenants._slots[name]


async def activate(client, merchant=42):
    response = await client.get(f"/i/m{merchant}/api/me", headers=admin(f"admin-{merchant}"))
    assert response.status_code == 200, response.text


async def asleep(app, name="m42"):
    assert await app.state.tenants._deactivate(slot(app, name)) is True
    assert slot(app, name).runtime is None


SPEC = ThreadSpec("chat:u:1", ThreadType.CHAT, Lane.INTERACTIVE, owner_user_id="u",
                  harness="openai_agents")


def chat(key, prompt="hello"):
    return Event(type="chat.message", source="test", payload={"text": prompt},
                 idempotency_key=key, thread_key=SPEC.key, user_id="u")


def live_store(app, name="m42"):
    return slot(app, name).runtime.app.state.lifecycle.store


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
        assert spy.starts.get("m42") is None  # nothing runs until there is a reason
        await activate(client)
        assert spy.starts["m42"] == 1
        assert state(pg_dsn)[0] is not None
        assert rows(pg_dsn, "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema = 'handler_m42' AND table_name = 'threads'")[0][0] == 1


# --- done when: idle tenants issue no queries ------------------------------


async def test_idle_tenant_issues_no_queries(base_env, pg_dsn):
    """Reads Postgres's own statement log, so it can't miss a query from any code path."""
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
        await until(lambda: "handler_m42" in since(offset))  # the detector sees an active tenant
        await asleep(app)
        await asyncio.sleep(0.3)
        offset = log.stat().st_size
        await asyncio.sleep(1.0)  # about 20 scans
        seen = since(offset)
    assert "tenant_state" in seen  # the host's one scan really ran
    for marker in ("handler_m42", "search_path TO", "FROM threads", "FROM events", "FROM schedules",
                   "FROM deliveries", "FROM settings"):
        assert marker not in seen, marker


async def test_idle_tenant_is_deactivated_after_the_idle_period(base_env, pg_dsn, spy):
    base_env["HANDLER_TENANT_IDLE_SECONDS"] = "0.3"
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await until(lambda: slot(app).runtime is None)
        assert spy.live["m42"] == 0
        await until(lambda: state(pg_dsn)[0] is None)  # cleared last, after next_wake_at
        await activate(client)  # and comes back on the next request
        assert spy.starts["m42"] == 2


# --- done when: crash recovery ---------------------------------------------


async def test_tenant_left_active_by_a_crash_restarts_at_boot(base_env, pg_dsn, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
    assert state(pg_dsn)[0] is not None  # a shutdown or crash leaves it set
    async with serve(base_env) as (app, client):
        await until(lambda: "m42" in app.state.tenants._slots and slot(app).runtime is not None)
    assert spy.starts["m42"] == 2


async def test_crash_between_next_wake_and_clearing_active_recovers(base_env, pg_dsn, spy, monkeypatch):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        wake_at = utcnow() + timedelta(hours=1)
        await live_store(app).create_schedule(Schedule(name="s", prompt="p", spec="every 1h",
                                                       next_run_at=wake_at))

        async def crash(name):
            raise RuntimeError("process died here")

        monkeypatch.setattr(app.state.registry, "clear_active", crash)
        await app.state.tenants._deactivate(slot(app))
        active_since, next_wake = state(pg_dsn)
        assert active_since is not None and next_wake == wake_at
        assert slot(app).recover
        monkeypatch.undo()
    monkeypatch.setattr(server, "OpenAIAgentsHarness", FakeHarness)
    monkeypatch.setattr(server, "codex_binary", lambda: (_ for _ in ()).throw(FileNotFoundError()))
    starts = spy.starts["m42"]
    spy2 = RuntimeSpy(monkeypatch)
    async with serve(base_env) as (app, client):
        await until(lambda: "m42" in app.state.tenants._slots and slot(app).runtime is not None)
    assert starts == 1 and spy2.starts["m42"] == 1


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


# --- done when: scheduled wake-ups -----------------------------------------


async def test_schedule_wakes_an_inactive_tenant_and_fires(base_env, pg_dsn, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        due = utcnow() + timedelta(seconds=1)
        await live_store(app).create_schedule(Schedule(
            name="s", prompt="scheduled work", spec="every 1h", next_run_at=due,
            harness="openai_agents"))
        await asleep(app)
        assert state(pg_dsn) == (None, due)
        await until(lambda: spy.starts["m42"] == 2)
        await until(lambda: any("scheduled work" in run for run in FakeHarness.runs))
        assert state(pg_dsn)[0] is not None


async def test_next_wake_uses_the_earliest_of_schedule_and_outbox_retry(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        store = live_store(app)
        later, sooner = utcnow() + timedelta(hours=2), utcnow() + timedelta(minutes=30)
        await store.create_schedule(Schedule(name="s", prompt="p", spec="every 1h", next_run_at=later))
        await store.enqueue_delivery("crontal", "k1", {"a": 1})
        await store._dq("UPDATE deliveries SET next_attempt_at = ? WHERE key = 'k1'", (sooner,))
        tenants = app.state.tenants
        lifecycle = slot(app).runtime.app.state.lifecycle
        lifecycle.outbox.sinks["crontal"] = object()  # a sink this runtime can deliver to
        await tenants._deactivate(slot(app))
        assert state(pg_dsn) == (None, sooner)


async def test_outbox_retry_wakes_an_inactive_tenant(base_env, pg_dsn, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await asleep(app)
        due = utcnow() + timedelta(milliseconds=200)
        with psycopg.connect(pg_dsn) as conn:
            conn.execute("UPDATE handler_control.tenant_state SET next_wake_at = %s "
                         "WHERE name = 'm42'", (due,))
        await until(lambda: spy.starts["m42"] == 2)


# --- races around one tenant's lifecycle -----------------------------------


async def test_concurrent_activations_make_one_runtime(base_env, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        responses = await asyncio.gather(*(
            client.get("/i/m42/api/me", headers=admin("admin-42")) for _ in range(25)))
        assert {r.status_code for r in responses} == {200}
        assert spy.starts["m42"] == 1 and spy.peak["m42"] == 1


async def test_request_during_deactivation_gets_a_fresh_runtime(base_env, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        spy.hold_stop["m42"] = asyncio.Event()
        spy.stopping["m42"] = asyncio.Event()
        stopping = asyncio.create_task(app.state.tenants._deactivate(slot(app)))
        await asyncio.wait_for(spy.stopping["m42"].wait(), 5)
        request = asyncio.create_task(client.get("/i/m42/api/me", headers=admin("admin-42")))
        await asyncio.sleep(0.1)
        assert not request.done() and spy.live["m42"] == 1  # waits; no second runtime
        spy.hold_stop.pop("m42").set()
        assert (await asyncio.wait_for(request, 10)).status_code == 200
        assert await stopping is True
        assert spy.starts["m42"] == 2 and spy.peak["m42"] == 1


async def test_notification_during_deactivation_restarts_the_tenant(base_env, pg_dsn, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        spy.hold_stop["m42"] = asyncio.Event()
        spy.stopping["m42"] = asyncio.Event()
        stopping = asyncio.create_task(app.state.tenants._deactivate(slot(app)))
        await asyncio.wait_for(spy.stopping["m42"].wait(), 5)
        await until(lambda: "handler_m42" not in app.state.registry.hub._wakes)
        cli = PostgresStore(pg_dsn, schema="handler_m42")
        try:
            await cli.put(chat("during-stop", "during stop"), SPEC)
        finally:
            await cli.close()
        spy.hold_stop.pop("m42").set()
        await stopping
        await until(lambda: any("during stop" in run for run in FakeHarness.runs))
        assert spy.peak["m42"] == 1


async def test_activation_while_deactivation_is_flushing_keeps_the_tenant(base_env, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        sync = slot(app).runtime.app.state.lifecycle.workspace_sync
        flushing, release = asyncio.Event(), asyncio.Event()
        sync.mark_dirty("thread")

        async def slow_flush():
            flushing.set()
            await release.wait()
            sync._dirty.clear()
            return 1

        sync.flush = slow_flush
        stopping = asyncio.create_task(app.state.tenants._deactivate(slot(app)))
        await asyncio.wait_for(flushing.wait(), 5)
        request = asyncio.create_task(client.get("/i/m42/api/me", headers=admin("admin-42")))
        await asyncio.sleep(0.1)
        assert not request.done()
        release.set()
        assert (await asyncio.wait_for(request, 10)).status_code == 200
        assert await stopping is False  # the request arrived, so the stop was abandoned
        assert slot(app).runtime is not None and spy.starts["m42"] == 1


async def test_a_failed_flush_keeps_the_tenant_active(base_env):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        sync = slot(app).runtime.app.state.lifecycle.workspace_sync
        sync.mark_dirty("thread")

        async def failing():
            return 0

        sync.flush = failing
        assert await app.state.tenants._deactivate(slot(app)) is False
        assert slot(app).runtime is not None
        sync._dirty.clear()


async def test_deactivation_does_not_hold_up_other_tenants(base_env, spy):
    async with serve(base_env) as (app, client):
        await create(client, 1)
        await create(client, 2)
        await activate(client, 1)
        await activate(client, 2)
        spy.hold_stop["m1"] = asyncio.Event()
        spy.stopping["m1"] = asyncio.Event()
        stopping = asyncio.create_task(app.state.tenants._deactivate(slot(app, "m1")))
        await asyncio.wait_for(spy.stopping["m1"].wait(), 5)
        response = await asyncio.wait_for(client.get("/i/m2/api/me", headers=admin("admin-2")), 5)
        assert response.status_code == 200
        await create(client, 3)
        await asyncio.wait_for(activate(client, 3), 10)  # a new start is not blocked either
        spy.hold_stop.pop("m1").set()
        await stopping


# --- deactivation checks ---------------------------------------------------


async def test_in_flight_run_blocks_deactivation(base_env):
    FakeHarness.gate = asyncio.Event()
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await live_store(app).put(chat("busy"), SPEC)
        await until(lambda: FakeHarness.runs)
        assert await app.state.tenants._deactivate(slot(app)) is False
        FakeHarness.gate.set()


async def test_pending_events_block_deactivation(base_env, monkeypatch):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        lifecycle = slot(app).runtime.app.state.lifecycle
        monkeypatch.setattr(lifecycle.dispatcher, "claim_once", lambda: asyncio.sleep(0, 0))
        await asyncio.sleep(0.05)
        await lifecycle.store.put(chat("queued"), SPEC)
        assert await app.state.tenants._deactivate(slot(app)) is False


async def test_due_outbox_row_blocks_deactivation(base_env):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        lifecycle = slot(app).runtime.app.state.lifecycle
        await lifecycle.store.enqueue_delivery("crontal", "k1", {"a": 1})
        lifecycle.outbox.sinks["crontal"] = object()
        assert await app.state.tenants._deactivate(slot(app)) is False
        lifecycle.outbox.sinks.pop("crontal")
        assert await app.state.tenants._deactivate(slot(app)) is True  # no sink: not its to send


async def test_open_sse_stream_blocks_deactivation(base_env):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        started, gone = asyncio.Event(), asyncio.Event()

        async def receive():
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            started.set()

        scope = {"type": "http", "method": "GET", "path": "/i/m42/api/stream", "raw_path": b"/i/m42/api/stream",
                 "root_path": "", "scheme": "http", "query_string": b"", "server": ("h", 80),
                 "client": ("c", 1), "http_version": "1.1",
                 "headers": [(b"authorization", b"Bearer admin-42")]}
        stream = asyncio.create_task(app.state.tenants(scope, receive, send))
        await asyncio.wait_for(started.wait(), 5)
        assert await app.state.tenants._deactivate(slot(app)) is False
        gone.set()
        await asyncio.wait_for(stream, 5)
        assert await app.state.tenants._deactivate(slot(app)) is True


# --- lost wake-ups ---------------------------------------------------------


async def test_notify_activates_an_inactive_tenant_and_runs_its_work(base_env, pg_dsn, spy):
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
        assert spy.starts["m42"] == 2


async def test_notify_missed_while_the_listener_is_down_is_found_by_the_scan(base_env, pg_dsn, spy):
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
        await until(lambda: any("missed notify" in run for run in FakeHarness.runs))


async def test_listener_reconnect_triggers_a_scan_and_keeps_notifications(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await asleep(app)
        app.state.tenants.scan_seconds = 3600  # only the reconnect can prompt a scan now
        await asyncio.sleep(0.1)
        cli = PostgresStore(pg_dsn, schema="handler_m42")
        await cli.get_thread("x")
        rows(pg_dsn, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = current_database() AND query ILIKE 'LISTEN%%'")
        try:
            await cli.put(chat("gap", "written in the gap"), SPEC)
        finally:
            await cli.close()
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
        cli._marks_wake = False
        await cli.put(chat("silent", "silent work"), SPEC)
    finally:
        await cli.close()
    assert state(pg_dsn) == (None, None)
    async with serve(base_env) as (app, client):
        await until(lambda: any("silent work" in run for run in FakeHarness.runs))


async def test_the_delayed_boot_sweep_catches_writes_during_a_dyno_overlap(base_env, pg_dsn):
    base_env["HANDLER_TENANT_BOOT_GRACE_SECONDS"] = "0.5"
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await asleep(app)
    async with serve(base_env) as (app, client):
        await asyncio.sleep(0.1)  # booted; the old dyno is still writing
        hub = app.state.registry.hub
        hub._listener.cancel()
        await asyncio.gather(hub._listener, return_exceptions=True)
        old = PostgresStore(pg_dsn, schema="handler_m42")
        try:
            await old.get_thread("x")
            old._marks_wake = False
            await old.put(chat("overlap", "overlap work"), SPEC)
        finally:
            await old.close()
        await until(lambda: any("overlap work" in run for run in FakeHarness.runs))


async def test_failed_activation_is_retried_durably(base_env, pg_dsn, spy, monkeypatch):
    monkeypatch.setattr("handler.runtime.tenancy.host.ACTIVATION_RETRY_SECONDS", 0.2)
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        await asleep(app)
        spy.fail_next.add("m42")
        response = await client.get("/i/m42/api/me", headers=admin("admin-42"))
        assert response.status_code == 503  # the sender retries; nothing was accepted and lost
        assert response.headers["retry-after"] == "5"
        active_since, wake = state(pg_dsn)
        assert active_since is None and wake is not None
        await until(lambda: slot(app).runtime is not None)  # the scan retried without a request


async def test_crontal_notification_on_an_inactive_tenant_runs(base_env, pg_dsn, spy):
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
        assert spy.starts["m42"] == 2


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


async def test_inbound_email_to_an_inactive_tenant_is_stored(base_env, pg_dsn, spy):
    secrets = {"HANDLER_ADMIN_KEY": "admin-42", "HANDLER_EMAIL_SECRET": "mail-42"}
    async with serve(base_env) as (app, client):
        await create(client, secrets=secrets)
        await activate(client)
        await asleep(app)
        response = await post_email(client, "m42", "mail-42", raw_email())
        assert response.status_code < 300, response.text
        assert spy.starts["m42"] == 2
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.events")[0][0] == 1


async def test_inbound_email_during_deactivation_is_stored(base_env, pg_dsn, spy):
    secrets = {"HANDLER_ADMIN_KEY": "admin-42", "HANDLER_EMAIL_SECRET": "mail-42"}
    async with serve(base_env) as (app, client):
        await create(client, secrets=secrets)
        await activate(client)
        spy.hold_stop["m42"] = asyncio.Event()
        spy.stopping["m42"] = asyncio.Event()
        stopping = asyncio.create_task(app.state.tenants._deactivate(slot(app)))
        await asyncio.wait_for(spy.stopping["m42"].wait(), 5)
        post = asyncio.create_task(post_email(client, "m42", "mail-42", raw_email()))
        await asyncio.sleep(0.1)
        assert not post.done()
        spy.hold_stop.pop("m42").set()
        response = await asyncio.wait_for(post, 10)
        await stopping
        assert response.status_code < 300, response.text
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.events")[0][0] == 1
        assert spy.peak["m42"] == 1


# --- pinned tenants --------------------------------------------------------


async def add_mailbox(app, name="m42", *, enabled=True):
    from handler.runtime.integrations.mailbox.config import Mailbox
    from handler.runtime.integrations.mailbox.store import MailStore

    # Loopback port 1: the poller's connection is refused at once, with no network.
    mailbox = Mailbox(email="sales@example.com", username="sales@example.com", label="Sales",
                      imap_host="127.0.0.1", imap_port=1, imap_security="tls",
                      smtp_host="127.0.0.1", smtp_port=1, smtp_security="tls", enabled=enabled)
    await MailStore(live_store(app, name)).put_mailbox(mailbox)
    return mailbox


async def test_a_mailbox_integration_alone_does_not_pin_a_tenant(base_env, spy):
    base_env["HANDLER_TENANT_IDLE_SECONDS"] = "0.2"
    async with serve(base_env) as (app, client):
        await create(client, integrations=["mailbox"])  # what Crontal pushes to every paid tenant
        await asyncio.sleep(0.3)
        assert spy.starts.get("m42") is None  # asleep at boot: nothing to poll
        await activate(client)
        await until(lambda: slot(app).runtime is None)  # and it sleeps again when idle


async def test_a_connected_mailbox_pins_and_removing_it_unpins(base_env, spy):
    from handler.runtime.integrations.mailbox.store import MailStore

    base_env["HANDLER_TENANT_IDLE_SECONDS"] = "0.2"
    async with serve(base_env) as (app, client):
        await create(client, integrations=["mailbox"])
        await activate(client)
        mailbox = await add_mailbox(app)
        await until(lambda: slot(app).pinned)
        await asyncio.sleep(0.5)
        assert slot(app).runtime is not None  # past the idle period, still awake
        await MailStore(live_store(app)).delete_mailbox(mailbox.id)
        await until(lambda: slot(app).runtime is None)
        assert not slot(app).pinned


async def test_a_disabled_mailbox_does_not_pin(base_env, spy):
    base_env["HANDLER_TENANT_IDLE_SECONDS"] = "0.2"
    async with serve(base_env) as (app, client):
        await create(client, integrations=["mailbox"])
        await activate(client)
        await add_mailbox(app, enabled=False)
        await until(lambda: slot(app).runtime is None)


async def test_a_tenant_with_a_mailbox_is_woken_at_boot_and_kept_awake(base_env, spy):
    base_env["HANDLER_TENANT_IDLE_SECONDS"] = "0.2"
    async with serve(base_env) as (app, client):
        await create(client, integrations=["mailbox"])
        await activate(client)
        await add_mailbox(app)
        await until(lambda: slot(app).pinned)
    async with serve(base_env) as (app, client):
        await until(lambda: "m42" in app.state.tenants._slots and slot(app).runtime is not None)
        await asyncio.sleep(0.5)
        assert slot(app).runtime is not None and slot(app).pinned


async def test_an_ordinary_tenant_is_not_pinned(base_env, spy):
    async with serve(base_env) as (app, client):
        await create(client, integrations=["crontal"])
        await asyncio.sleep(0.2)
        assert spy.starts.get("m42") is None


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
        await until(lambda: spy.starts["m42"] == 1 and slot(app).runtime is None)
        assert (await client.get("/i/m42/api/me", headers=admin("rotated"))).status_code == 200
        assert (await client.get("/i/m42/api/me", headers=admin("admin-42"))).status_code == 401
        assert spy.envs["m42"]["HANDLER_RT_MODEL"] == "one"


async def test_disable_stops_the_tenant_and_enable_resumes_its_work(base_env, pg_dsn, spy):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        response = await client.post("/platform/tenants/42/disable", headers=PLATFORM)
        assert response.status_code == 200 and response.json()["enabled"] is False
        assert slot(app).runtime is None
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
    assert tables == {"tenant_schema_versions", "due_work"}


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
        assert store._marks_wake is False
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
