"""The host loops on real Postgres (step 3): isolation, invalidation, connections."""
import asyncio

import httpx

from handler.runtime import server
from handler.runtime.bus import BusMessage
from handler.runtime.gateway.secret_box import SecretBox
from handler.runtime.store.postgres_hub import PostgresHub
from handler.runtime.tenancy.registry import TenantRegistry
from handler.runtime.types import Event, Lane, ThreadSpec, ThreadType

from .test_shared_connections import ConnectionWatch, host_env, no_codex  # noqa: F401
from .test_tenant_registry import (  # noqa: F401  (fixtures)
    PLATFORM, SPEC, FakeHarness, RuntimeSpy, activate, admin, base_env, chat, context, create,
    fake_harness, live_store, rows, serve, spy, until,
)


async def test_two_tenants_running_at_once_share_nothing(base_env, pg_dsn):
    FakeHarness.gate = asyncio.Event()
    async with serve(base_env) as (app, client):
        await create(client, 1)
        await create(client, 2)
        await activate(client, 1)
        await activate(client, 2)
        one, two = context(app, "m1"), context(app, "m2")
        heard = {"m1": [], "m2": []}

        async def listen(name, bus):
            async with bus.subscribe("*") as queue:
                while True:
                    heard[name].append((await queue.get()).thread_key)

        listeners = [asyncio.create_task(listen(n, c.lifecycle.bus))
                     for n, c in (("m1", one), ("m2", two))]
        await asyncio.sleep(0.05)
        await one.store.put(chat("one", "tenant one"), SPEC)
        await two.store.put(Event(type="chat.message", source="t", payload={"text": "tenant two"},
                                  idempotency_key="two", thread_key="chat:u:2", user_id="u"),
                            ThreadSpec("chat:u:2", ThreadType.CHAT, Lane.INTERACTIVE,
                                       owner_user_id="u", harness="openai_agents"))
        await until(lambda: len(FakeHarness.runs) == 2)  # both running at once
        runner = app.state.services.runner
        assert runner.running("m1") == runner.running("m2") == 1
        # Stores: each sees only its own schema.
        assert [t.key for t in await one.store.list_threads()] == [SPEC.key]
        assert [t.key for t in await two.store.list_threads()] == ["chat:u:2"]
        # Bus: one host bus, each tenant's view hears only its own threads.
        two.lifecycle.bus.publish(BusMessage("chat:u:2", None, "probe"))
        await asyncio.sleep(0.05)
        assert "chat:u:2" not in heard["m1"] and SPEC.key not in heard["m2"]
        # Workspaces, sinks and contexts: distinct objects and roots per tenant.
        assert one.executor.config.workspaces_dir != two.executor.config.workspaces_dir
        assert not one.executor.config.workspaces_dir.is_relative_to(
            two.executor.config.workspaces_dir)
        assert one.outbox is not two.outbox and one.outbox.tenant == "m1"
        assert one.gateway is not two.gateway and one.store is not two.store
        assert set(app.state.services.cache.names()) == {"m1", "m2"}
        # Run tokens: each carries its own tenant and gateway.
        tokens = app.state.services.tokens
        issued = {tokens.tenant_of(t) for t in tokens._tokens}
        assert issued == {"m1", "m2"}
        for token, (_, _, gateway, tenant) in tokens._tokens.items():
            assert gateway is context(app, tenant).gateway
        FakeHarness.gate.set()
        await until(lambda: runner.active_count == 0)
        for task in listeners:
            task.cancel()
        await asyncio.gather(*listeners, return_exceptions=True)
    assert rows(pg_dsn, "SELECT count(*) FROM handler_m1.threads")[0][0] == 1
    assert rows(pg_dsn, "SELECT count(*) FROM handler_m2.threads")[0][0] == 1


async def other_process(pg_dsn):
    """A registry in another process (its own hub and connections): the CLI, say."""
    hub = PostgresHub(pg_dsn, max_size=2)
    await hub.start()
    return hub, TenantRegistry(hub, SecretBox("sealing-key"))


async def test_a_plan_change_from_another_process_applies_on_the_next_run(base_env, pg_dsn,
                                                                         monkeypatch):
    from .test_free_plan import ToolViewHarness

    ToolViewHarness.views, ToolViewHarness.created = [], []
    monkeypatch.setattr(server, "OpenAIAgentsHarness", ToolViewHarness)
    async with serve(base_env) as (app, client):
        ToolViewHarness.app = app
        await create(client, plan="free")
        await activate(client)
        await live_store(app).put(chat("before"), SPEC)
        await until(lambda: ToolViewHarness.created)
        assert "automation_create" not in ToolViewHarness.views[0]
        hub, registry = await other_process(pg_dsn)
        try:
            await registry.upsert_merchant(42, {"plan": "paid", "limits": {}}, {},
                                           lambda: "unused")  # NOTIFY handler_tenant_changed
        finally:
            await hub.close()
        await until(lambda: context(app) is not None
                    and context(app).limits.current.automations)
        await live_store(app).put(chat("after"), SPEC)
        await until(lambda: len(ToolViewHarness.created) == 2)
        assert "automation_create" in ToolViewHarness.views[1]


async def test_disabling_from_another_process_stops_claims(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        hub, registry = await other_process(pg_dsn)
        try:
            await registry.set_enabled("m42", False)
        finally:
            await hub.close()
        await until(lambda: not app.state.services.tenants.get("m42").enabled)
        from handler.runtime.store.postgres import PostgresStore

        cli = PostgresStore(pg_dsn, schema="handler_m42")  # its own context was dropped
        try:
            await cli.put(chat("off", "while disabled"), SPEC)
        finally:
            await cli.close()
        await asyncio.sleep(0.5)
        assert not FakeHarness.runs  # queued, not run
        assert context(app) is None


async def test_settings_saved_elsewhere_drop_the_context(base_env, pg_dsn, spy):
    from handler.runtime.store.postgres import PostgresStore

    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        builds = spy.starts["m42"]
        hub = PostgresHub(pg_dsn, max_size=2)
        other = PostgresStore(pg_dsn, schema="handler_m42", hub=hub)
        try:
            saved = await other.get_settings("threads")  # saved at the tenant's first build
            assert await other.put_settings("threads", saved.value, updated_by="cli",
                                            expected_version=saved.version)
        finally:
            await other.close()
            await hub.close()
        await until(lambda: context(app) is None)
        await activate(client)
        assert spy.starts["m42"] == builds + 1


async def test_fifty_tenants_under_load_stay_within_twelve_connections(tmp_path, pg_dsn, no_codex,
                                                                       monkeypatch):
    monkeypatch.setattr(server, "OpenAIAgentsHarness", FakeHarness)
    names = ["main", *(f"t{n}" for n in range(49))]
    env = {**host_env(tmp_path, pg_dsn, names), "HANDLER_TENANT_CACHE_SIZE": "16",
           "HANDLER_CLAIM_POLL_SECONDS": "0.1", "HANDLER_OUTBOX_POLL_SECONDS": "0.1",
           "HANDLER_TIMER_POLL_SECONDS": "0.1", "HANDLER_GLOBAL_MAX_RUNS": "8"}
    host = server.create_host(server._parse_args(["serve"], env), env)

    async def tenant_load(client, name):
        base = f"/i/{name}"
        for i in range(2):
            assert (await client.post(f"{base}/api/users", headers=admin(f"{name}-admin"),
                                      json={"id": f"u{i}", "display_name": "U"})).status_code == 201
            assert (await client.get(f"{base}/api/threads",
                                     headers=admin(f"{name}-admin"))).status_code == 200
        response = await client.post(f"{base}/api/chat",
                                     headers={**admin(f"{name}-admin"), "X-Handler-User": "u0"},
                                     json={"text": f"hello from {name}"})
        assert response.status_code < 300, response.text

    async with ConnectionWatch(pg_dsn) as watch:
        async with host.router.lifespan_context(host):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host),
                                         base_url="http://handler", timeout=60) as client:
                await asyncio.gather(*(tenant_load(client, n) for n in names))
                await until(lambda: sum("hello from" in r for r in FakeHarness.runs) == 50,
                            timeout=120)
            assert len(host.state.services.cache) <= 16 + 8  # size, plus contexts in use
    assert watch.samples > 10
    assert 2 <= watch.peak <= 12, f"peak {watch.peak}"
    assert rows(pg_dsn, "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() AND backend_type = 'client backend'")[0][0] == 0
