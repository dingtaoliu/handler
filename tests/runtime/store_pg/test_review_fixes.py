"""Fixes from the pre-release review of feat/multi-tenant."""
import asyncio
import logging
import threading
from datetime import datetime, timezone

import pytest

from handler.runtime import server, storage
from handler.runtime.dispatcher import Dispatcher
from handler.runtime.documents import limiter
from handler.runtime.tenancy.limits import LiveLimits, PlanLimits, check_limit_keys, period_of
from handler.runtime.tenancy.run_gate import RunGate
from handler.runtime.types import Event, Lane, ThreadSpec, ThreadType

from .test_plan_limits import held, input_event, spec_for, used
from .test_tenant_registry import (  # noqa: F401  (fixtures)
    PLATFORM,
    FakeHarness,
    activate,
    admin,
    base_env,
    create,
    fake_harness,
    live_store,
    rows,
    serve,
    slot,
    spy,
    until,
)


# --- 1: run-gate slots never leak ---------------------------------------------


class FailingStore:
    def __init__(self):
        self.calls = 0

    async def claim(self, owner, lane, limit, lease_seconds):
        self.calls += 1
        raise ConnectionError("pool timeout")

    async def has_claimable(self, lane):
        return True

    async def work_state(self):
        from handler.runtime.store.base import WorkState

        return WorkState(frozenset({Lane.INTERACTIVE, Lane.BACKGROUND}))


async def test_a_failed_claim_gives_its_gate_slots_back():
    gate = RunGate(2, {"free": 1, "paid": 2})
    gate.configure("t", "free")
    dispatcher = Dispatcher(FailingStore(), executor=None, gate=gate, gate_tenant="t",
                            lane_capacity={Lane.INTERACTIVE: 2, Lane.BACKGROUND: 1})
    for _ in range(5):  # the host loop logs a failed claim and carries on
        assert await dispatcher.claim_once() == 0
    assert dispatcher.store.calls >= 5
    assert gate.total == 0 and gate.running("t") == 0
    assert gate.reserve("t", 1) == 1  # a free tenant at cap 1 can still run


async def test_with_the_registry_off_every_dispatcher_shares_one_gate(tmp_path, monkeypatch):
    """Changed on purpose (Codex review finding 2): env-defined instances used to get no
    gate, so each instance's HANDLER_MAX_CONCURRENT added up across the dyno. They now
    share one host gate, configured paid as the registry would import them."""
    from tests.runtime.test_multi_instance import config

    monkeypatch.setattr(server, "codex_binary", lambda: (_ for _ in ()).throw(FileNotFoundError()))
    env = {**config(tmp_path), "HANDLER_GLOBAL_MAX_RUNS": "4", "HANDLER_MAX_CONCURRENT": "20"}
    host = server.create_host(server._parse_args(["serve"], env), env)
    async with host.router.lifespan_context(host):
        services = host.state.services
        contexts = [services.cache.cached(name) for name in ("main", "bss")]
    gate = host.state.run_gate
    assert services.runner.gate is gate  # one claim loop, one gate, for every instance
    assert [c.max_concurrent for c in contexts] == [20, 20]  # still applies beneath
    assert gate.max_runs == 4 and gate._cap("main") == gate._cap("bss") == 3  # paid


async def test_env_instances_together_stay_under_the_host_cap(tmp_path, monkeypatch):
    from tests.runtime.test_multi_instance import config

    monkeypatch.setattr(server, "codex_binary", lambda: (_ for _ in ()).throw(FileNotFoundError()))
    FakeHarness.gate = asyncio.Event()
    env = {**config(tmp_path), "HANDLER_GLOBAL_MAX_RUNS": "2", "HANDLER_MAX_CONCURRENT": "20"}
    host = server.create_host(server._parse_args(["serve"], env), env)
    async with host.router.lifespan_context(host):
        gate = host.state.run_gate
        apps = {name: host.state.services.cache.cached(name).app for name in ("main", "bss")}
        for name, app in apps.items():
            store = app.state.lifecycle.store
            for n in range(3):
                spec = ThreadSpec(f"chat:u:{n}", ThreadType.CHAT, Lane.INTERACTIVE,
                                  owner_user_id="u", harness="openai_agents")
                await store.put(Event(type="chat.message", source="test",
                                      payload={"text": f"{name} {n}"},
                                      idempotency_key=f"{name}-{n}", thread_key=spec.key,
                                      user_id="u"), spec)
        await until(lambda: len(FakeHarness.runs) == 2)
        await asyncio.sleep(0.3)
        assert len(FakeHarness.runs) == 2 and gate.total == 2  # not 2 per instance
        assert sum(app.state.lifecycle.dispatcher.active_count for app in apps.values()) == 2
        FakeHarness.gate.set()
        await until(lambda: len(FakeHarness.runs) == 6)
        await until(lambda: gate.total == 0)
    FakeHarness.gate = None


# --- 2: max_runs is validated ------------------------------------------------------


@pytest.mark.parametrize("bad", ["two", 0, -1, 1.5, True])
def test_max_runs_must_be_a_positive_integer(bad):
    with pytest.raises(ValueError, match="max_runs"):
        check_limit_keys({"max_runs": bad}, "tenant")
    check_limit_keys({"max_runs": 2}, "tenant")
    check_limit_keys({"max_runs": None}, "tenant")


async def test_the_platform_api_refuses_a_bad_max_runs(base_env):
    async with serve(base_env) as (app, client):
        for bad in ("two", 0, -3):
            response = await client.put("/platform/tenants/5", headers=PLATFORM,
                                        json={"limits": {"max_runs": bad}})
            assert response.status_code == 409, bad
        assert (await client.put("/platform/tenants/5", headers=PLATFORM,
                                 json={"limits": {"max_runs": 2}})).status_code == 201
        assert app.state.run_gate._cap("m5") == 2


async def test_a_bad_max_runs_already_stored_stops_startup(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client)
    import psycopg

    with psycopg.connect(pg_dsn) as conn:
        conn.execute("""UPDATE handler_control.tenants SET limits = '{"max_runs": "two"}'""")
    with pytest.raises(ValueError, match="max_runs"):
        async with serve(base_env):
            pass


# --- 3 and 8: plan changes seen by load(), limits resolved on change only -----------


async def hold_one(app):
    store = live_store(app)
    for n in range(1):
        assert await store.put(input_event(f"f{n}", "chat.message", thread_key=f"chat:u:{n}"),
                               spec_for(ThreadType.CHAT, f"chat:u:{n}")) is True
    over = await store.put(input_event("h", "email.received", thread_key="email:h"),
                           spec_for(ThreadType.EMAIL, "email:h"))
    assert over and over.action == "held"


FREE_HOLD = {"plan": "free", "limits": {"monthly_inputs": 1, "at_limit": "hold", "email": True}}


async def test_a_plan_change_made_elsewhere_is_applied_by_load(base_env, pg_dsn, spy):
    async with serve(base_env) as (app, client):
        await create(client, **FREE_HOLD)
        await activate(client)
        await hold_one(app)
        # Another process (or a write racing the minute's reload) changes the plan.
        await app.state.registry.upsert_merchant(42, {"plan": "paid", "limits": {}}, {},
                                                 lambda: "unused")
        await app.state.tenants.load()
        await until(lambda: rows(pg_dsn, "SELECT count(*) FROM handler_m42.held_inputs")[0][0] == 0)
        assert live_store(app).limits.current.plan == "paid"
        assert slot(app).stale  # its new config waits for the next quiet restart
        # The platform's own report afterwards still sees nothing left to do.
        await app.state.tenants.tenant_changed("m42")
        assert spy.starts["m42"] == 1


async def test_limits_are_resolved_only_when_they_change(base_env, monkeypatch):
    async with serve(base_env) as (app, client):
        await create(client)
        tenants = app.state.tenants
        calls = []
        real = tenants._limits_for
        tenants._limits_for = lambda tenant: calls.append(tenant.name) or real(tenant)
        await activate(client)
        for _ in range(20):
            await client.get("/i/m42/api/me", headers=admin("admin-42"))
            await client.get("/i/m42/healthz")
        await tenants.load()
        await tenants.tick()
        assert calls == []
        await client.put("/platform/tenants/42", headers=PLATFORM, json={"limits": {"max_runs": 3}})
        assert calls == ["m42"]


# --- 5 and 10: held inputs first in a new month, and shown as queued --------------


async def test_a_new_months_first_input_comes_after_the_held_ones(tenant, clock):
    from .test_plan_limits import fill_and_hold

    await fill_and_hold(tenant, 2)  # 3 used, 2 held
    announced = []
    tenant.on_routed = lambda event, spec: announced.append(event.idempotency_key)
    clock.now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    outcome = await tenant.put(input_event("new-month", "email.received", thread_key="email:n"),
                               spec_for(ThreadType.EMAIL, "email:n"))
    assert outcome is True  # 2 held + this one fit in the new month's 3
    assert await held(tenant) == []
    assert await used(tenant, period_of(clock())) == 3
    assert announced[:2] == ["held0", "held1"] and announced[-1] == "new-month"


async def test_a_new_input_that_no_longer_fits_waits_behind_the_held_ones(tenant, clock):
    from .test_plan_limits import fill_and_hold

    await fill_and_hold(tenant, 3)
    clock.now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    outcome = await tenant.put(input_event("late", "email.received", thread_key="email:l"),
                               spec_for(ThreadType.EMAIL, "email:l"))
    assert outcome and outcome.action == "held"
    assert [key for key, _ in await held(tenant)] == ["late"]


async def test_released_inputs_are_announced_as_queued(tenant, clock):
    from .test_plan_limits import fill_and_hold

    await fill_and_hold(tenant, 2)
    announced = []
    tenant.on_routed = lambda event, spec: announced.append((event.idempotency_key, spec.key))
    clock.now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert await tenant.release_held_inputs() == 2
    assert announced == [("held0", "email:h0"), ("held1", "email:h1")]


@pytest.fixture
async def tenant(pg_dsn, clock):
    from handler.runtime.store.postgres import PostgresStore
    from handler.runtime.store.postgres_hub import PostgresHub

    hub = PostgresHub(pg_dsn)
    store = PostgresStore(pg_dsn, schema="handler_q", clock=clock, hub=hub)
    store.limits = LiveLimits(PlanLimits("free", monthly_inputs=3))
    try:
        await store.get_thread("x")
        yield store
    finally:
        await store.close()
        await hub.close()


# --- 6: the document-read cap can be raised, and a stuck read shows up ----------


def test_the_read_cap_can_be_raised():
    try:
        assert limiter.configure_read_limit("6") == 6
        assert limiter.reader_pool().settings.size == 6
    finally:
        limiter.configure_read_limit(None)
    assert limiter.DEFAULT_READ_LIMIT == 1


# The waiting-for-a-slot log is covered with the reader pool in test_document_read_cap.py.


# --- 7: storage checks don't walk the tree on every write ---------------------------


def test_workspace_checks_walk_the_tree_once_not_per_write(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    (root / "t").mkdir(parents=True)
    walks = []
    real = storage.directory_size
    monkeypatch.setattr(storage, "directory_size", lambda path: walks.append(path) or real(path))
    storage.register_workspace_limit(root, lambda: 10_000)
    try:
        for n in range(50):
            storage.ensure_workspace_room(root / "t" / f"f{n}", 100)
            (root / "t" / f"f{n}").write_bytes(b"x" * 100)
        assert len(walks) == 1
        with pytest.raises(storage.StorageLimitReached):
            storage.ensure_workspace_room(root / "t" / "big", 5_001)
    finally:
        storage.unregister_workspace_limit(root)


async def test_a_stale_total_is_recounted_in_a_worker_thread(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    root.mkdir()
    used = storage.UsedBytes(root)
    await used.prime()
    (root / "f").write_bytes(b"x" * 300)  # written behind its back
    monkeypatch.setattr(storage, "RECOUNT_SECONDS", 0.0)
    loop_thread = threading.get_ident()
    threads = []
    real = storage.directory_size
    monkeypatch.setattr(storage, "directory_size",
                        lambda path: threads.append(threading.get_ident()) or real(path))
    used.used()
    await until(lambda: used.used() == 300)
    assert threads and loop_thread not in threads


def test_upload_totals_track_saves_and_deletes(tmp_path, monkeypatch):
    from handler.runtime.uploads import UploadStore

    store = UploadStore(tmp_path / "uploads", limit=lambda: 1000)
    walks = []
    real = storage.directory_size
    monkeypatch.setattr(storage, "directory_size", lambda path: walks.append(path) or real(path))
    metas = [store.save("u", f"{n}.txt", "text/plain", [b"x" * 300]) for n in range(3)]
    with pytest.raises(storage.StorageLimitReached):
        store.save("u", "more.txt", "text/plain", [b"x" * 300])
    store.delete(metas[0])
    store.save("u", "again.txt", "text/plain", [b"x" * 300])
    assert len(walks) == 1
