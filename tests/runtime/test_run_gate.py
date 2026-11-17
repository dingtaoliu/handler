"""The process-wide run gate: global cap, per-plan caps, chats first, paid first, no starvation."""
import asyncio

import pytest

from handler.runtime.tenancy.run_gate import RunGate, parse_plan_caps
from handler.runtime.types import Lane


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def gate(max_runs=4, caps="free:1,paid:3", **kwargs):
    instance = RunGate(max_runs, parse_plan_caps(caps), **kwargs)
    instance.configure("paid_a", "paid")
    instance.configure("paid_b", "paid")
    instance.configure("free_a", "free")
    instance.configure("free_b", "free")
    return instance


def test_global_cap_holds():
    g = gate(max_runs=4)
    assert g.reserve("paid_a", 3) == 3
    assert g.reserve("paid_b", 3) == 1
    assert g.reserve("free_a", 1) == 0
    assert g.total == 4


def test_plan_cap_and_per_tenant_override():
    g = gate(max_runs=10)
    assert g.reserve("free_a", 5) == 1
    assert g.reserve("free_a", 1) == 0
    g.configure("free_b", "free", max_runs=2)
    assert g.reserve("free_b", 5) == 2


def test_unknown_plan_gets_the_lowest_cap():
    g = gate(max_runs=10)
    g.configure("odd", "trial")
    assert g.reserve("odd", 5) == 1


def test_paid_waiter_goes_before_a_free_tenant():
    g = gate(max_runs=1)
    assert g.reserve("free_a", 1) == 1
    paid, free = asyncio.Event(), asyncio.Event()
    g.enqueue("free_b", free)
    g.enqueue("paid_a", paid)
    g.release("free_a")
    assert paid.is_set() and free.is_set()  # both retry; only the paid tenant wins
    assert g.reserve("free_b", 1) == 0
    assert g.reserve("paid_a", 1) == 1


def test_a_tenant_with_fewer_runs_goes_first():
    g = gate(max_runs=4)
    assert g.reserve("paid_a", 3) == 3
    assert g.reserve("paid_b", 1) == 1
    g.enqueue("paid_a", asyncio.Event())
    g.enqueue("paid_b", asyncio.Event())
    g.release("paid_a")
    assert g.reserve("paid_a", 1) == 0  # the busy tenant does not take it back
    assert g.reserve("paid_b", 1) == 1


def test_a_waiter_at_its_cap_does_not_block_others():
    g = gate(max_runs=4)
    assert g.reserve("free_a", 1) == 1
    g.enqueue("free_a", asyncio.Event())
    assert g.reserve("free_b", 1) == 1


def test_priority_cannot_starve_a_free_tenant():
    clock = Clock()
    g = gate(max_runs=1, clock=clock, max_wait=60)
    assert g.reserve("paid_a", 1) == 1
    g.enqueue("free_a", asyncio.Event())
    clock.now = 61.0
    g.enqueue("paid_b", asyncio.Event())  # arrives later, still ranks first by plan until aged
    g.release("paid_a")
    assert g.reserve("paid_b", 1) == 0
    assert g.reserve("free_a", 1) == 1


def test_a_gone_tenant_stops_holding_the_queue():
    g = gate(max_runs=1)
    g.enqueue("paid_a", asyncio.Event())
    assert g.reserve("free_a", 1) == 0
    g.dequeue("paid_a")
    assert g.reserve("free_a", 1) == 1


@pytest.mark.parametrize("raw", ["free", "free:0", "free:x", ":1"])
def test_bad_plan_caps_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_plan_caps(raw)


def test_dequeue_of_an_absent_tenant_wakes_nobody():
    g = gate()
    event = asyncio.Event()
    g.enqueue("free_a", event)
    g.dequeue("paid_a")  # dispatchers call this on every idle pass
    assert not event.is_set()


# --- lanes: interactive before background across tenants (owner decision 8) --------

BG, CHAT = Lane.BACKGROUND, Lane.INTERACTIVE


def test_a_waiting_chat_from_one_tenant_goes_before_an_earlier_background_wait():
    clock = Clock()
    g = gate(max_runs=1, clock=clock, max_wait=60)
    assert g.reserve("paid_b", 1, BG) == 1
    g.enqueue("paid_a", asyncio.Event(), BG)  # waited first
    clock.now = 5.0
    g.enqueue("free_a", asyncio.Event(), CHAT)  # a free chat, waiting since later
    g.release("paid_b")
    assert g.reserve("paid_a", 1, BG) == 0  # paid, earlier, fewer runs: still behind a chat
    assert g.reserve("free_a", 1, CHAT) == 1


def test_lanes_rank_before_plans_then_plans_before_runs():
    g = gate(max_runs=2)
    assert g.reserve("paid_b", 2, CHAT) == 2
    g.enqueue("paid_a", asyncio.Event(), BG)
    g.enqueue("free_a", asyncio.Event(), CHAT)
    g.enqueue("free_b", asyncio.Event(), BG)
    g.release("paid_b", 2)
    assert g.reserve("paid_a", 1, BG) == 1  # one slot is left after the chat waiting ahead
    assert g.reserve("free_b", 1, BG) == 0  # paid background ranked ahead of free background
    assert g.reserve("free_a", 1, CHAT) == 1


def test_a_tenants_own_background_wait_does_not_hold_back_its_chat():
    g = gate(max_runs=1)
    g.enqueue("paid_a", asyncio.Event(), BG)
    assert g.reserve("paid_a", 1, CHAT) == 1
    assert g.waiting("paid_a", BG) and not g.waiting("paid_a", CHAT)


def test_dequeue_of_one_lane_keeps_the_other_waiting():
    g = gate(max_runs=1)
    g.enqueue("paid_a", asyncio.Event(), BG)
    g.enqueue("paid_a", asyncio.Event(), CHAT)
    g.dequeue("paid_a", CHAT)
    assert g.waiting("paid_a", BG) and not g.waiting("paid_a", CHAT)
    g.dequeue("paid_a")
    assert not g.waiting("paid_a")


def test_background_gets_through_a_constant_stream_of_chats_after_the_bound():
    clock = Clock()
    g = gate(max_runs=1, clock=clock, max_wait=60)
    g.enqueue("paid_a", asyncio.Event(), BG)
    background_runs_at = []
    for second in range(0, 200, 5):  # a chat from some tenant is always waiting
        clock.now = float(second)
        g.enqueue("paid_b", asyncio.Event(), CHAT)
        if g.reserve("paid_a", 1, BG):
            background_runs_at.append(second)
            g.release("paid_a")
            g.enqueue("paid_a", asyncio.Event(), BG)  # more background work, waiting again
        assert g.reserve("paid_b", 1, CHAT) in (0, 1)
        if g.running("paid_b"):
            g.release("paid_b")
    assert background_runs_at == [60, 120, 180]  # once per bound, never starved


class HeldExecutor:
    """Records which tenant's run starts, and holds every run until released."""

    def __init__(self, tenant, started, release):
        self.tenant, self.started, self.release = tenant, started, release

    async def execute(self, lease):
        self.started.append((self.tenant, lease.thread_key))
        await self.release[lease.thread_key].wait()


async def test_dispatchers_admit_a_waiting_chat_before_an_earlier_background_run(tmp_path):
    from handler.runtime.dispatcher import Dispatcher
    from handler.runtime.store.sqlite import SQLiteStore
    from handler.runtime.types import Event, ThreadSpec, ThreadType

    g = RunGate(1, parse_plan_caps("free:1,paid:3"))
    started, release = [], {}
    stores, dispatchers, stop = {}, {}, asyncio.Event()
    for tenant in ("c", "a", "b"):
        g.configure(tenant, "paid")
        stores[tenant] = SQLiteStore(tmp_path / f"{tenant}.db")
        dispatchers[tenant] = Dispatcher(stores[tenant], HeldExecutor(tenant, started, release),
                                         gate=g, gate_tenant=tenant, idle_poll_seconds=0.05)

    async def put(tenant, key, lane):
        release[key] = asyncio.Event()
        kind = ThreadType.CHAT if lane == CHAT else ThreadType.AUTOMATION
        await stores[tenant].put(Event(type="chat.message", source="t", payload={"text": key},
                                       idempotency_key=key, thread_key=key, user_id="u"),
                                 ThreadSpec(key, kind, lane, owner_user_id="u", harness="x"))

    async def until(condition):
        for _ in range(500):
            if condition():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not reached")

    loops = [asyncio.create_task(d.run_forever(stop, shutdown_grace=0.1))
             for d in dispatchers.values()]
    try:
        await put("c", "chat:c", CHAT)
        await until(lambda: started == [("c", "chat:c")])  # the gate is full
        await put("a", "automation:a", BG)
        await until(lambda: g.waiting("a", BG))
        await put("b", "chat:b", CHAT)
        await until(lambda: g.waiting("b", CHAT))
        release["chat:c"].set()
        await until(lambda: len(started) == 2)
        assert started[1] == ("b", "chat:b")  # B's chat first, though A waited longer
        release["chat:b"].set()
        await until(lambda: len(started) == 3)
        assert started[2] == ("a", "automation:a")
        release["automation:a"].set()
    finally:
        stop.set()
        await asyncio.gather(*loops)
        for store in stores.values():
            await store.close()
