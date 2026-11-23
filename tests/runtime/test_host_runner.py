"""The host's one claim loop (runner.py): fair across tenants, lanes and plans.

Real SQLite stores, one per tenant, and the real RunGate; the executor only records
which thread ran and holds it until the test lets it finish.
"""
import asyncio

from handler.runtime.runner import HostRunner
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.tenancy.contexts import ContextCache, HostTenants, ServedTenant, TenantContext
from handler.runtime.tenancy.run_gate import RunGate
from handler.runtime.types import Event, Lane, ThreadSpec, ThreadType


class HeldExecutor:
    def __init__(self, name, store, started):
        self.name, self.store, self.started = name, store, started
        self.release: dict[str, asyncio.Event] = {}

    async def execute(self, lease):
        release = self.release.setdefault(lease.thread_key, asyncio.Event())
        self.started.append((self.name, lease.thread_key))
        await release.wait()
        events = await self.store.pending_events(lease.thread_key)
        await self.store.complete(lease, "run", [e.id for e in events])


async def host(tmp_path, plans: dict[str, str], gate: RunGate):
    cache = ContextCache()
    tenants = HostTenants(cache)
    runner = HostRunner(tenants, gate, poll_seconds=0.05)
    started: list = []
    stores, executors = {}, {}
    for name, plan in plans.items():
        store = SQLiteStore(tmp_path / f"{name}.db")
        executor = HeldExecutor(name, store, started)
        cache.pin(TenantContext(name=name, key="local", store=store, executor=executor,
                                lane_capacity={Lane.INTERACTIVE: 10, Lane.BACKGROUND: 10},
                                lease_seconds=60))
        tenants.serve(ServedTenant(name, "local", plan=plan, clock=store._clock))
        runner.local.register(name, store._clock)
        store._wake.listener = lambda name=name: runner.hint(name)
        gate.configure(name, plan)
        stores[name], executors[name] = store, executor
    return runner, stores, executors, started


async def put(store, key, lane):
    spec = ThreadSpec(key, ThreadType.CHAT if lane == Lane.INTERACTIVE else ThreadType.AUTOMATION,
                      lane, owner_user_id="u")
    await store.put(Event(type="chat.message", source="t", payload={"text": key},
                          idempotency_key=key, thread_key=key, user_id="u"), spec)


async def finish(runner, executor, key):
    executor.release.setdefault(key, asyncio.Event()).set()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not any(t.get_name() == f"run:{key}" for t in runner._tasks):
            return
    raise AssertionError(f"{key} did not finish")


async def close(runner, stores, executors):
    for executor in executors.values():
        for event in executor.release.values():
            event.set()
    await runner.drain(2)
    for store in stores.values():
        await store.close()


async def test_with_the_gate_full_another_tenants_chat_goes_before_a_background_flood(tmp_path):
    gate = RunGate(1, {"free": 1, "paid": 3})
    runner, stores, executors, started = await host(tmp_path, {"a": "paid", "b": "paid"}, gate)
    try:
        for n in range(5):
            await put(stores["a"], f"bg{n}", Lane.BACKGROUND)
        assert await runner.claim_once() == 1  # the one slot: a's background run
        await put(stores["b"], "chat", Lane.INTERACTIVE)
        assert await runner.claim_once() == 0  # gate full: b waits, ranked first
        await asyncio.sleep(0.01)
        await finish(runner, executors["a"], started[0][1])
        assert await runner.claim_once() == 1
        await asyncio.sleep(0.01)  # the run's task starts
        assert started[1] == ("b", "chat")  # admitted next, before a's 4 waiting
    finally:
        await close(runner, stores, executors)


async def test_a_free_tenants_flood_cannot_starve_a_paid_one(tmp_path):
    gate = RunGate(2, {"free": 1, "paid": 2}, max_wait=3600)
    runner, stores, executors, started = await host(tmp_path, {"free": "free", "paid": "paid"},
                                                    gate)
    try:
        for n in range(10):
            await put(stores["free"], f"flood{n}", Lane.INTERACTIVE)
        await runner.claim_once()
        for n in range(3):
            await put(stores["paid"], f"paid{n}", Lane.INTERACTIVE)
        await runner.claim_once()
        # Both tenants run at once: free at its plan cap (1), paid in the other slot.
        assert gate.running("free") == 1 and gate.running("paid") == 1
        for _ in range(4):  # finish whatever runs; each freed slot is re-admitted
            for name, key in list(started):
                if any(t.get_name() == f"run:{key}" for t in runner._tasks):
                    await finish(runner, executors[name], key)
            await runner.claim_once()
        paid = [i for i, (name, _) in enumerate(started) if name == "paid"]
        flood = [i for i, (name, _) in enumerate(started) if name == "free"]
        assert len(paid) == 3  # every paid chat ran while the flood was still queued
        assert len(flood) < 10 and max(paid) < len(started)
    finally:
        await close(runner, stores, executors)


async def test_two_tenants_runs_progress_concurrently_in_the_loop(tmp_path):
    gate = RunGate(4, {"free": 1, "paid": 2})
    runner, stores, executors, started = await host(tmp_path, {"a": "paid", "b": "paid"}, gate)
    stop = asyncio.Event()
    loop = asyncio.create_task(runner.run(stop))
    try:
        await put(stores["a"], "a1", Lane.INTERACTIVE)
        await put(stores["b"], "b1", Lane.INTERACTIVE)
        for _ in range(200):
            if len(started) == 2:
                break
            await asyncio.sleep(0.01)
        assert {name for name, _ in started} == {"a", "b"}  # both running, neither waits
        assert runner.running("a") == runner.running("b") == 1
    finally:
        stop.set()
        for executor in executors.values():
            for event in executor.release.values():
                event.set()
        await loop
        for store in stores.values():
            await store.close()


async def test_a_disabled_tenant_is_not_claimed(tmp_path):
    gate = RunGate(4, {"free": 1, "paid": 2})
    runner, stores, executors, started = await host(tmp_path, {"a": "paid"}, gate)
    try:
        runner.tenants.serve(ServedTenant("a", "local", enabled=False))
        await put(stores["a"], "a1", Lane.INTERACTIVE)
        assert await runner.claim_once() == 0 and not started
        runner.tenants.serve(ServedTenant("a", "local", enabled=True,
                                          clock=stores["a"]._clock))
        runner.hint("a")
        assert await runner.claim_once() == 1
    finally:
        await close(runner, stores, executors)
