"""Postgres-only: multiple store instances ("processes") sharing one database."""

import asyncio
from dataclasses import replace

import pytest

from handler.runtime.store.postgres import PostgresCore
from handler.runtime.types import Lane


@pytest.fixture
async def pair(pg_dsn, clock):
    a, b = PostgresCore(pg_dsn, clock=clock), PostgresCore(pg_dsn, clock=clock)
    await asyncio.gather(a.get_thread("x"), b.get_thread("x"))  # concurrent schema setup
    try:
        yield a, b
    finally:
        await asyncio.gather(a.close(), b.close())


async def test_two_instances_never_double_claim(pair, spec, event):
    a, b = pair
    for i in range(40):
        await a.put(event(f"e{i}"), replace(spec, key=f"t{i}"))
    claimed = []

    async def claimer(store, owner):
        while batch := await store.claim(owner, Lane.INTERACTIVE, 3, 60):
            claimed.extend(batch)
            await asyncio.sleep(0)

    await asyncio.gather(claimer(a, "a"), claimer(b, "b"), claimer(a, "a2"), claimer(b, "b2"))
    keys = [lease.thread_key for lease in claimed]
    assert len(keys) == len(set(keys)) == 40
    assert all(lease.version == 1 for lease in claimed)


async def test_notify_wakes_other_instance(pair, spec, event):
    a, b = pair
    await b.wait_for_work(0.1)
    waiter = asyncio.create_task(b.wait_for_work(30))
    await asyncio.sleep(0.2)
    assert not waiter.done()
    await a.put(event("first"), spec)
    await asyncio.wait_for(waiter, 5)


async def test_complete_wakes_other_instance(pair, spec, event):
    a, b = pair
    queued = event("first")
    await a.put(queued, spec)
    lease, = await a.claim("w", Lane.INTERACTIVE, 1, 60)
    await b.wait_for_work(0.2)
    waiter = asyncio.create_task(b.wait_for_work(30))
    await asyncio.sleep(0.2)
    await a.complete(lease, "run", [queued.id])
    await asyncio.wait_for(waiter, 5)


async def test_use_after_close_raises(pg_dsn):
    store = PostgresCore(pg_dsn)
    await store.get_thread("x")
    await store.close()
    with pytest.raises(RuntimeError):
        await store.get_thread("x")
