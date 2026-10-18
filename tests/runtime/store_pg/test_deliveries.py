"""The deliveries outbox behaves identically on both database backends."""
from handler.runtime.store.deliveries import DEAD, DELIVERED, PENDING


async def test_enqueue_is_idempotent_per_sink_and_key(domain_store):
    store = domain_store
    assert await store.enqueue_delivery("a", "k1", {"n": 1})
    assert not await store.enqueue_delivery("a", "k1", {"n": 2})
    assert await store.enqueue_delivery("b", "k1", {"n": 3})
    row = await store.get_delivery("a", "k1")
    assert row.payload == {"n": 1} and row.status == PENDING and row.attempts == 0
    assert len(await store.list_deliveries()) == 2


async def test_claim_leases_retries_and_dead_letters(domain_store, clock):
    store = domain_store
    await store.enqueue_delivery("a", "k1", {"n": 1})
    await store.enqueue_delivery("other", "k2", {"n": 2})
    (claimed,) = await store.claim_deliveries(["a"], lease_seconds=60)
    assert claimed.key == "k1" and claimed.payload == {"n": 1}
    # Leased: not claimable again until the lease runs out.
    assert await store.claim_deliveries(["a"]) == []
    clock.advance(61)
    assert [d.key for d in await store.claim_deliveries(["a"])] == ["k1"]

    await store.delivery_failed("a", "k1", "HTTP 503", 30)
    row = await store.get_delivery("a", "k1")
    assert row.status == PENDING and row.attempts == 1 and row.last_error == "HTTP 503"
    assert await store.claim_deliveries(["a"]) == []
    clock.advance(31)
    assert [d.attempts for d in await store.claim_deliveries(["a"])] == [1]

    await store.delivery_failed("a", "k1", "HTTP 400", None)
    row = await store.get_delivery("a", "k1")
    assert row.status == DEAD and row.attempts == 2
    clock.advance(3600)
    assert await store.claim_deliveries(["a"]) == []
    assert [d.key for d in await store.list_deliveries(DEAD)] == ["k1"]


async def test_success_is_final(domain_store, clock):
    store = domain_store
    await store.enqueue_delivery("a", "k1", {})
    await store.claim_deliveries(["a"])
    await store.delivery_succeeded("a", "k1")
    row = await store.get_delivery("a", "k1")
    assert row.status == DELIVERED and row.attempts == 1 and row.delivered_at is not None
    clock.advance(3600)
    assert await store.claim_deliveries(["a"]) == []
    assert await store.claim_deliveries([]) == []
