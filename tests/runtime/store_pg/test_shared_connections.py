"""Tenants share one pool and LISTEN connection (real Postgres, multi-tenant Phase 1)."""
import asyncio
import re
from pathlib import Path

import httpx
import psycopg
import pytest

from handler.runtime import server
from handler.runtime.store.postgres import CHANNEL, PostgresCore, PostgresStore
from handler.runtime.store.postgres_hub import CONTROL_SCHEMA, SHARED_CHANNEL, PostgresHub
from handler.runtime.types import Lane

SRC = Path(__file__).resolve().parents[3] / "src" / "handler" / "runtime"


@pytest.fixture
async def hub(pg_dsn):
    instance = PostgresHub(pg_dsn)
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
async def tenants(hub, pg_dsn, clock):
    a = PostgresStore(pg_dsn, schema="handler_a", clock=clock, hub=hub)
    b = PostgresStore(pg_dsn, schema="handler_b", clock=clock, hub=hub)
    try:
        await asyncio.gather(a.get_thread("x"), b.get_thread("x"))
        yield a, b
    finally:
        await asyncio.gather(a.close(), b.close())


class ConnectionWatch:
    """Samples how many connections this database has, from outside the code under test."""

    def __init__(self, dsn):
        self.dsn, self.peak, self.samples = dsn, 0, 0
        self._stop = asyncio.Event()

    async def _run(self):
        conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True)
        async with conn:
            while not self._stop.is_set():
                row = await (await conn.execute(
                    # Client connections only: an autovacuum worker also carries
                    # the database's name, and is no connection Handler holds.
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND backend_type = 'client backend'")).fetchone()
                self.peak = max(self.peak, row[0])
                self.samples += 1
                await asyncio.sleep(0.005)

    async def __aenter__(self):
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc):
        self._stop.set()
        await self._task


def rows(pg_dsn, query, *params):
    with psycopg.connect(pg_dsn) as conn:
        return conn.execute(query, params).fetchall()


# --- scoping ---------------------------------------------------------------


async def test_an_unscoped_query_fails(tenants, hub, spec, event):
    a, _ = tenants
    await a.put(event("visible-to-a"), spec)
    async with hub.pool.connection() as conn:
        assert (await (await conn.execute("SHOW search_path")).fetchone())["search_path"] == CONTROL_SCHEMA
        for table in ("threads", "events", "runs", "messages"):
            with pytest.raises(psycopg.errors.UndefinedTable):
                await conn.execute(f"SELECT * FROM {table}")
            await conn.rollback()


async def test_store_code_that_skips_scoping_fails(tenants):
    a, _ = tenants
    a._scope = None  # what a path bypassing the chokepoint would do
    with pytest.raises(psycopg.errors.UndefinedTable):
        await a.get_thread("x")


async def test_control_schema_holds_no_tenant_tables(tenants, pg_dsn):
    found = {r[0] for r in rows(
        pg_dsn, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        CONTROL_SCHEMA)}
    assert found == {"tenant_schema_versions", "due_work", "tenant_state"}  # control data only


async def test_pool_connections_are_transactional(tenants, hub):
    # SET LOCAL has no effect in autocommit.
    async with hub.pool.connection() as conn:
        assert conn.autocommit is False


async def test_scope_ends_with_the_transaction(tenants, hub):
    a, _ = tenants
    async with a._conn() as conn:
        assert (await (await conn.execute("SELECT current_schema() AS s")).fetchone())["s"] == "handler_a"
    with pytest.raises(RuntimeError):
        async with a._conn() as conn:
            raise RuntimeError("rolled back")
    async with hub.pool.connection() as conn:
        assert (await (await conn.execute("SHOW search_path")).fetchone())["search_path"] == CONTROL_SCHEMA


async def test_nested_transaction_keeps_the_scope(tenants, spec, event):
    a, _ = tenants
    await a.put(event("e"), spec)
    async with a._conn() as conn:
        async with conn.transaction():  # mailbox _script does this inside _conn
            cursor = await conn.execute("SELECT key FROM threads")
            assert [r["key"] for r in await cursor.fetchall()] == [spec.key]


async def test_tenants_see_only_their_own_rows(tenants, spec, event):
    a, b = tenants
    assert await a.put(event("same-key", payload={"tenant": "a"}), spec)
    assert await b.put(event("same-key", payload={"tenant": "b"}), spec)  # idempotency per tenant
    assert (await a.pending_events(spec.key))[0].payload == {"tenant": "a"}
    assert (await b.pending_events(spec.key))[0].payload == {"tenant": "b"}
    lease, = await a.claim("w", Lane.INTERACTIVE, 5, 60)
    assert lease.thread_key == spec.key
    assert (await b.get_thread(spec.key)).lease_owner is None


async def test_schema_name_is_quoted_as_an_identifier(hub, pg_dsn, spec, event):
    store = PostgresStore(pg_dsn, schema="select", hub=hub)
    try:
        await store.put(event("keyword"), spec)
        assert await store.get_thread(spec.key) is not None
        assert rows(pg_dsn, "SELECT count(*) FROM \"select\".threads")[0][0] == 1
    finally:
        await store.close()


async def test_no_schema_on_a_hub_means_public(hub, pg_dsn, spec, event):
    store = PostgresStore(pg_dsn, hub=hub)
    try:
        await store.put(event("in-public"), spec)
        assert rows(pg_dsn, "SELECT count(*) FROM public.threads")[0][0] == 1
    finally:
        await store.close()


# --- one connection across tenants -----------------------------------------


@pytest.fixture
async def single_connection(pg_dsn, clock, spec, event):
    hub = PostgresHub(pg_dsn, min_size=1, max_size=1)
    a = PostgresStore(pg_dsn, schema="tenant_a", clock=clock, hub=hub)
    b = PostgresStore(pg_dsn, schema="tenant_b", clock=clock, hub=hub)
    try:
        await a.put(event("a", payload={"tenant": "a"}), spec)
        await b.put(event("b", payload={"tenant": "b"}), spec)
        yield hub, a, b
    finally:
        await asyncio.gather(a.close(), b.close())
        await hub.close()


async def backend_pid(store):
    async with store._conn() as conn:
        return (await (await conn.execute("SELECT pg_backend_pid() AS p")).fetchone())["p"]


async def test_repeated_sql_alternating_tenants_on_one_connection(single_connection, spec):
    hub, a, b = single_connection
    assert await backend_pid(a) == await backend_pid(b)
    for _ in range(40):  # past psycopg's auto-prepare threshold of 5 for one query text
        assert [e.payload for e in await a.pending_events(spec.key)] == [{"tenant": "a"}]
        assert [e.payload for e in await b.pending_events(spec.key)] == [{"tenant": "b"}]
    async with hub.pool.connection() as conn:
        assert conn.prepare_threshold is None


async def test_prepared_statements_would_not_leak_tenants_either(single_connection, spec):
    """Isolation doesn't rest on the setting: Postgres re-resolves names per search_path."""
    hub, a, b = single_connection
    async with hub.pool.connection() as conn:
        conn.prepare_threshold = 0
    for _ in range(10):
        assert [e.payload for e in await a.pending_events(spec.key)] == [{"tenant": "a"}]
        assert [e.payload for e in await b.pending_events(spec.key)] == [{"tenant": "b"}]
    async with hub.pool.connection() as conn:
        count = (await (await conn.execute(
            "SELECT count(*) AS n FROM pg_prepared_statements")).fetchone())["n"]
        conn.prepare_threshold = None
    assert count > 0  # the statements really were server-side prepared


async def test_select_star_works_when_tenant_columns_differ_in_order(single_connection):
    """With auto-prepare this fails: "cached plan must not change result type"."""
    _, a, b = single_connection
    async with a._conn() as conn:
        await conn.execute("CREATE TABLE probe (x INT, y TEXT)")
        await conn.execute("INSERT INTO probe VALUES (1, 'a')")
    async with b._conn() as conn:
        await conn.execute("CREATE TABLE probe (y TEXT, x INT)")
        await conn.execute("INSERT INTO probe VALUES ('b', 2)")

    async def star(store):
        async with store._conn() as conn:
            return await (await conn.execute("SELECT * FROM probe")).fetchall()

    for _ in range(20):
        assert await star(a) == [{"x": 1, "y": "a"}]
        assert await star(b) == [{"y": "b", "x": 2}]


# --- notifications ---------------------------------------------------------


async def test_notification_wakes_only_the_matching_tenant(tenants, pg_dsn, spec, event):
    a, b = tenants
    await asyncio.sleep(0.05)
    a._wake.clear()
    b._wake.clear()
    await a.put(event("wake-a"), spec)
    a._wake.clear()  # drop the local wake; only the notification could set it again
    await asyncio.sleep(0.3)
    assert a._wake.is_set()
    assert not b._wake.is_set()


async def test_unknown_payload_is_ignored(tenants, pg_dsn):
    a, b = tenants
    a._wake.clear()
    b._wake.clear()
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_notify(%s, 'handler_nobody')", (SHARED_CHANNEL,))
    await asyncio.sleep(0.2)
    assert not a._wake.is_set() and not b._wake.is_set()


async def test_a_cli_store_with_its_own_connections_wakes_the_host(tenants, pg_dsn, spec, event):
    a, _ = tenants
    a._wake.clear()
    cli = PostgresStore(pg_dsn, schema="handler_a")
    try:
        await cli.put(event("from-cli"), spec)
        await asyncio.wait_for(a.wait_for_work(5), 6)
        assert len(await a.claim("w", Lane.INTERACTIVE, 5, 60)) == 1
    finally:
        await cli.close()


async def test_one_listen_connection_regardless_of_tenants(hub, pg_dsn):
    stores = [PostgresStore(pg_dsn, schema=f"handler_t{n}", hub=hub) for n in range(5)]
    try:
        await asyncio.gather(*(s.get_thread("x") for s in stores))
        listening = rows(pg_dsn, "SELECT count(*) FROM pg_stat_activity "
                                 "WHERE datname = current_database() AND query ILIKE 'LISTEN%%'")
        assert listening[0][0] == 1
    finally:
        await asyncio.gather(*(s.close() for s in stores))


async def test_tenant_stops_being_woken_after_close(hub, pg_dsn, tenants):
    a, _ = tenants
    assert "handler_a" in hub._wakes
    await a.close()
    assert "handler_a" not in hub._wakes


# --- schema versions -------------------------------------------------------


async def test_schema_version_is_recorded_per_tenant(tenants, pg_dsn):
    found = rows(pg_dsn, f"SELECT schema_name, component FROM {CONTROL_SCHEMA}.tenant_schema_versions")
    assert sorted(found) == [("handler_a", "core"), ("handler_b", "core")]


async def test_ddl_is_skipped_when_current(tenants, hub, pg_dsn):
    a, _ = tenants
    async with a._conn() as conn:
        await conn.execute("DROP INDEX runs_started")
    await a.close()
    again = PostgresStore(pg_dsn, schema="handler_a", hub=hub)
    try:
        await again.get_thread("x")
        assert rows(pg_dsn, "SELECT count(*) FROM pg_indexes WHERE schemaname = 'handler_a' "
                            "AND indexname = 'runs_started'")[0][0] == 0
    finally:
        await again.close()


async def test_ddl_is_applied_when_the_sql_changed(tenants, hub, pg_dsn):
    a, _ = tenants
    async with a._conn() as conn:
        await conn.execute("DROP INDEX runs_started")
        await conn.execute(f"UPDATE {CONTROL_SCHEMA}.tenant_schema_versions SET version = 'older' "
                           "WHERE schema_name = 'handler_a'")
    await a.close()
    again = PostgresStore(pg_dsn, schema="handler_a", hub=hub)
    try:
        await again.get_thread("x")
        assert rows(pg_dsn, "SELECT count(*) FROM pg_indexes WHERE schemaname = 'handler_a' "
                            "AND indexname = 'runs_started'")[0][0] == 1
    finally:
        await again.close()


async def test_many_new_tenants_set_up_on_a_small_pool(pg_dsn):
    hub = PostgresHub(pg_dsn, max_size=2)
    stores = [PostgresStore(pg_dsn, schema=f"handler_n{n}", hub=hub) for n in range(10)]
    try:
        await asyncio.wait_for(asyncio.gather(*(s.get_thread("x") for s in stores)), 30)
        assert not rows(pg_dsn, "SELECT 1 FROM pg_locks WHERE locktype = 'advisory'")
    finally:
        await asyncio.gather(*(s.close() for s in stores))
        await hub.close()


async def test_mailbox_tables_are_versioned_in_the_tenant_schema(tenants, pg_dsn):
    from handler.runtime.integrations.mailbox.store import MailStore

    a, _ = tenants
    assert await MailStore(a).list_mailboxes() == []
    assert rows(pg_dsn, "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'handler_a' AND table_name = 'mail_accounts'")[0][0] == 1
    assert ("handler_a", "mailbox") in rows(
        pg_dsn, f"SELECT schema_name, component FROM {CONTROL_SCHEMA}.tenant_schema_versions")


# --- connection budget -----------------------------------------------------


async def test_ten_tenants_under_load_stay_within_twelve_connections(pg_dsn, clock, spec, event):
    hub = PostgresHub(pg_dsn)
    stores = [PostgresStore(pg_dsn, schema=f"handler_t{n}", clock=clock, hub=hub) for n in range(10)]

    async def tenant_work(store, n):
        for i in range(6):
            await store.put(event(f"{n}-{i}"), spec)
            leases = await store.claim(f"w{n}", Lane.INTERACTIVE, 1, 60)
            async with store._conn() as conn:
                await conn.execute("SELECT pg_sleep(0.02)")  # hold connections past the pool's size
            await store.get_thread(spec.key)
            for lease in leases:
                await store.complete(lease, f"run-{i}", [])

    try:
        async with ConnectionWatch(pg_dsn) as watch:
            await asyncio.gather(*(tenant_work(s, n) for s in stores
                                   for n in range(4)), return_exceptions=False)
        assert watch.samples > 10
        assert 2 <= watch.peak <= 12
        assert watch.peak >= 10, f"the pool was never saturated (peak {watch.peak})"
    finally:
        await asyncio.gather(*(s.close() for s in stores))
        await hub.close()
    assert rows(pg_dsn, "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid()")[0][0] == 0


def host_env(tmp_path, pg_dsn, names):
    env = {
        "HANDLER_INSTANCES": ",".join(names),
        "HANDLER_RT_DATA_DIR": str(tmp_path / "main"),
        "HANDLER_ADMIN_KEY": "main-admin",
        "HANDLER_DB_SCHEMA": "handler_crontal",
        "DATABASE_URL": pg_dsn,
        "HANDLER_DB_POOL_MAX": "3",  # production's value: must not shrink the shared pool
        "OPENAI_API_KEY": "fake-no-network",
        "HANDLER_DEFAULT_HARNESS": "openai_agents",
    }
    for name in names:
        if name != "main":
            env[f"HANDLER_I_{name.upper()}__HANDLER_ADMIN_KEY"] = f"{name}-admin"
    return env


@pytest.fixture
def no_codex(monkeypatch):
    def missing():
        raise FileNotFoundError("test has no codex")

    monkeypatch.setattr(server, "codex_binary", missing)


@pytest.fixture
def quiet_host(monkeypatch, no_codex):
    """No dispatcher or scheduler: the seeded events must not start real runs."""

    async def idle(self, stop, *args):
        await stop.wait()

    monkeypatch.setattr(server.Dispatcher, "run_forever", idle)
    monkeypatch.setattr(server.HostTimer, "run", idle)


def admin(name):
    return {"Authorization": f"Bearer {name}-admin"}


async def test_host_with_ten_tenants_stays_within_twelve_connections(
    tmp_path, pg_dsn, no_codex
):
    # Every background service is real here: dispatchers, schedulers, housekeeping.
    names = ["main", *(f"t{n}" for n in range(9))]
    env = host_env(tmp_path, pg_dsn, names)
    host = server.create_host(server._parse_args(["serve"], env), env)

    async def tenant_load(client, name):
        for i in range(8):
            base = f"/i/{name}"
            assert (await client.post(f"{base}/api/users", headers=admin(name),
                                      json={"id": f"u{i}", "display_name": "U"})).status_code == 201
            assert (await client.get(f"{base}/api/settings/persona", headers=admin(name))).status_code == 200
            assert (await client.get(f"{base}/api/threads", headers=admin(name))).status_code == 200

    async with ConnectionWatch(pg_dsn) as watch:
        async with host.router.lifespan_context(host):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host),
                                         base_url="http://handler") as client:
                await asyncio.gather(*(tenant_load(client, n) for n in names))
    assert watch.samples > 10
    assert 2 <= watch.peak <= 12, f"peak {watch.peak}"
    assert rows(pg_dsn, "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid()")[0][0] == 0


async def test_existing_tenants_keep_schemas_data_and_urls(tmp_path, pg_dsn, quiet_host, clock, spec, event):
    # Written the way the deployed image writes today: own pool and options search_path.
    names = {"main": "handler_crontal", "bss": "handler_bss", "yeup": "handler_yeup"}
    for name, schema in names.items():
        old = PostgresStore(pg_dsn, schema=schema, clock=clock)
        await old.put(event(f"before-{name}", payload={"tenant": name}), spec)
        await old.close()

    env = host_env(tmp_path, pg_dsn, list(names))
    host = server.create_host(server._parse_args(["serve"], env), env)
    async with host.router.lifespan_context(host):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host),
                                     base_url="http://handler") as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/i/nope/healthz")).status_code == 404
            for name in names:
                assert (await client.get(f"/i/{name}/healthz")).status_code == 200
                for other in names:
                    status = (await client.get(f"/i/{name}/api/me", headers=admin(other))).status_code
                    assert (status == 200) == (name == other)
                listed = await client.get(f"/i/{name}/api/threads", headers=admin(name))
                assert [t["key"] for t in listed.json()] == [spec.key]
            assert (await client.get("/api/threads", headers=admin("main"))).status_code == 200
    created = {r[0] for r in rows(pg_dsn, "SELECT schema_name FROM information_schema.schemata "
                                          "WHERE schema_name LIKE 'handler%%'")}
    assert created == {*names.values(), CONTROL_SCHEMA}
    assert rows(pg_dsn, "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public'")[0][0] == 0
    for name, schema in names.items():
        assert rows(pg_dsn, f"SELECT payload->>'tenant' FROM {schema}.events") == [(name,)]


# --- rollout and rollback --------------------------------------------------


async def test_previous_image_runs_against_a_database_this_code_touched(tenants, pg_dsn, clock, spec, event):
    a, _ = tenants
    await a.put(event("written-by-new"), spec)
    await a.close()
    # Rollback: the old store ignores the control schema and finds its tables unchanged.
    old = PostgresStore(pg_dsn, schema="handler_a", clock=clock)
    try:
        assert await old.get_thread(spec.key) is not None
        await old.put(event("written-by-old"), spec)
        assert len(await old.pending_events(spec.key)) == 2
        leases = await old.claim("w", Lane.INTERACTIVE, 5, 60)
        assert [lease.thread_key for lease in leases] == [spec.key]
    finally:
        await old.close()


async def test_old_per_schema_notification_falls_back_to_polling(tenants, pg_dsn, clock, spec, event):
    a, _ = tenants
    other_dyno = PostgresStore(pg_dsn, schema="handler_a", clock=clock)
    try:
        await other_dyno.put(event("on-old-dyno"), spec)
        await asyncio.sleep(0.2)
        a._wake.clear()
        # All an old dyno would send: the per-schema channel, which the hub isn't on.
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            conn.execute("SELECT pg_notify(%s, '')", (f"{CHANNEL}_handler_a",))
        await asyncio.sleep(0.3)
        assert not a._wake.is_set()
        await a.wait_for_work(0.05)  # the dispatcher's idle poll, 30 s in production
        assert len(await a.claim("w", Lane.INTERACTIVE, 5, 60)) == 1
    finally:
        await other_dyno.close()


async def test_shared_channel_differs_from_every_per_schema_channel():
    assert SHARED_CHANNEL != CHANNEL and not SHARED_CHANNEL.startswith(f"{CHANNEL}_handler_")


# --- single instance -------------------------------------------------------


async def test_a_store_without_a_hub_keeps_its_own_connections(pg_dsn, spec, event):
    store = PostgresStore(pg_dsn, schema="handler_solo")
    try:
        assert store._hub is None and store._scope is None and store._pool.max_size == 5
        await store.put(event("solo"), spec)
        await asyncio.sleep(0.05)
        assert store._listening.is_set()
    finally:
        await store.close()
    assert not rows(pg_dsn, "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                    CONTROL_SCHEMA)


async def test_single_instance_host_builds_no_hub(tmp_path):
    env = {"HANDLER_RT_DATA_DIR": str(tmp_path), "DATABASE_URL": "host=x dbname=y"}
    configs = server.instance_configs(server._parse_args(["serve"], env), env)
    assert server._shared_hubs(configs, env) == {}


async def test_host_builds_one_hub_per_database(tmp_path, pg_dsn):
    env = host_env(tmp_path, pg_dsn, ["main", "bss", "yeup"])
    env["HANDLER_I_YEUP__DATABASE_URL"] = "host=elsewhere dbname=y"
    env["HANDLER_DB_SHARED_POOL_MAX"] = "7"
    configs = server.instance_configs(server._parse_args(["serve"], env), env)
    hubs = server._shared_hubs(configs, env)
    assert set(hubs) == {pg_dsn, "host=elsewhere dbname=y"}
    assert hubs[pg_dsn].pool.max_size == 7


# --- source guards ---------------------------------------------------------


def _python_sources():
    return [path for path in SRC.rglob("*.py") if "sqlite" not in path.name]


def test_only_the_chokepoints_open_postgres_connections():
    opens = {}
    for path in _python_sources():
        text = path.read_text()
        found = len(re.findall(r"AsyncConnection\.connect\(|psycopg\.connect\(", text))
        if found:
            opens[path.name] = found
    assert opens == {"postgres.py": 1, "postgres_hub.py": 1}  # the two LISTEN loops


def test_store_transactions_go_through_the_scoped_chokepoint():
    text = (SRC / "store/postgres.py").read_text()
    assert text.count("self._pool.connection()") == 2  # _scoped, and _setup without a hub
    assert not re.search(r"\._pool\b", "".join(
        p.read_text() for p in _python_sources() if p.name not in ("postgres.py", "postgres_hub.py")))


def test_no_session_level_state_on_shared_connections():
    banned = re.compile(r"pg_advisory_lock|pg_try_advisory_lock\(|TEMP(ORARY)? TABLE|WITH HOLD|"
                        r"execute\(\s*[\"']SET (?!LOCAL)", re.IGNORECASE)
    offenders = [p.name for p in _python_sources() if banned.search(p.read_text())]
    offenders += [p.name for p in (SRC / "store").glob("schema_pg*.sql") if banned.search(p.read_text())]
    assert offenders == []
