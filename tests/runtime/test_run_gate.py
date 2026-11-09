"""The process-wide run gate: global cap, per-plan caps, paid first, no starvation."""
import asyncio

import pytest

from handler.runtime.tenancy.run_gate import RunGate, parse_plan_caps


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
