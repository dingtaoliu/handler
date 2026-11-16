"""Parked threads and redelivery behave identically on both database backends."""
from datetime import timedelta

from handler.runtime.store.deliveries import DEAD, PENDING
from handler.runtime.types import Event, Lane, Schedule, ThreadSpec, ThreadType

SPEC = ThreadSpec("automation:s1", ThreadType.AUTOMATION, Lane.BACKGROUND,
                  owner_user_id="u", harness="openai_agents")


def fired(key, clock):
    return Event("automation.fired", "test", {"name": "job"}, key,
                 thread_key=SPEC.key, user_id="u", received_at=clock())


async def park(store, clock):
    await store.put(fired("e1", clock), SPEC)
    (lease,) = await store.claim("w", Lane.BACKGROUND, 1, 60)
    assert await store.fail(lease, "HarnessError: provider down", None)


async def test_new_events_on_a_parked_thread_queue_and_notice_once(domain_store, clock):
    store = domain_store
    noticed = []

    async def hook(event, spec):
        noticed.append(event.idempotency_key)

    store.on_parked_event = hook
    await park(store, clock)
    for key in ("e2", "e3"):
        assert await store.put(fired(key, clock), SPEC)
    assert noticed == ["e2"]
    assert [e.idempotency_key for e in await store.pending_events(SPEC.key)] == ["e1", "e2", "e3"]
    assert await store.claim("w", Lane.BACKGROUND, 1, 60) == []  # parked: nothing runs
    (row,) = await store.parked_threads()
    assert row["thread_key"] == SPEC.key and row["pending_events"] == 3
    assert row["attempts"] == 1 and row["last_error"] == "HarnessError: provider down"
    # A scheduled firing on the parked thread is kept too, silently (already told).
    schedule = Schedule(name="job", prompt="go", spec="every 1h", next_run_at=clock())
    await store.create_schedule(schedule)
    thread = ThreadSpec(SPEC.key, SPEC.type, SPEC.lane, harness="openai_agents")
    assert await store.fire_schedule(schedule, schedule.next_run_at,
                                     clock() + timedelta(hours=1), fired("e4", clock), thread)
    assert noticed == ["e2"] and len(await store.pending_events(SPEC.key)) == 4
    # Retried, run, parked again: the next new event is noticed again.
    assert await store.revive_thread(SPEC.key)
    assert not await store.parked_threads()
    (lease,) = await store.claim("w", Lane.BACKGROUND, 1, 60)
    assert await store.fail(lease, "again", None)
    await store.put(fired("e5", clock), SPEC)
    assert noticed == ["e2", "e5"]


async def test_redeliver_one_or_all_dead_rows(domain_store):
    store = domain_store
    for key in ("k1", "k2", "k3"):
        await store.enqueue_delivery("a", key, {"n": key})
    for key in ("k1", "k2"):
        await store.delivery_failed("a", key, "HTTP 500", None)
    await store.enqueue_delivery("b", "k9", {})
    await store.delivery_failed("b", "k9", "x", None)
    assert await store.redeliver("a", "k1") == 1
    row = await store.get_delivery("a", "k1")
    assert row.status == PENDING and row.attempts == 0
    assert await store.redeliver("a", "k3") == 0  # pending, not dead
    assert await store.redeliver("a") == 1
    assert {d.key for d in await store.claim_deliveries(["a"])} == {"k1", "k2", "k3"}
    assert (await store.get_delivery("b", "k9")).status == DEAD
