"""Two schemas in one database are isolated (throwaway cluster only)."""

import asyncio

import psycopg
import pytest

from handler.runtime.store.postgres import PostgresCore, PostgresStore
from handler.runtime.types import Lane


@pytest.fixture
async def pair(pg_dsn):
    a = PostgresStore(pg_dsn, schema="handler_a")
    b = PostgresStore(pg_dsn, schema="handler_b")
    try:
        yield a, b
    finally:
        await asyncio.gather(a.close(), b.close())


async def test_tables_live_in_their_own_schema(pair, pg_dsn, spec, event):
    a, b = pair
    await a.put(event("only-a"), spec)
    await b.get_thread("x")  # force setup of b
    with psycopg.connect(pg_dsn) as conn:
        rows = conn.execute(
            "SELECT table_schema FROM information_schema.tables "
            "WHERE table_name = 'threads'").fetchall()
    assert sorted(r[0] for r in rows) == ["handler_a", "handler_b"]  # nothing in public


async def test_data_is_isolated(pair, spec, event):
    a, b = pair
    assert await a.put(event("same-key"), spec)
    assert await b.put(event("same-key"), spec)  # idempotency key is per schema
    assert await a.get_thread(spec.key) is not None
    await b.close()
    c = PostgresCore(b._dsn, schema="handler_c")
    try:
        assert await c.get_thread(spec.key) is None
        assert await c.claim("w", Lane.INTERACTIVE, 5, 60) == []
    finally:
        await c.close()
    leases = await a.claim("w", Lane.INTERACTIVE, 5, 60)
    assert [lease.thread_key for lease in leases] == [spec.key]


async def test_notify_does_not_cross_schemas(pair, spec, event):
    a, b = pair
    await asyncio.gather(a.get_thread("x"), b.get_thread("x"))
    await asyncio.sleep(0.2)
    a._wake.clear()
    b._wake.clear()
    await a.put(event("wake-a"), spec)
    a._wake.clear()  # drop the local wake; only a NOTIFY could set it again
    await asyncio.sleep(0.5)
    assert a._wake.is_set()  # own notification arrives
    assert not b._wake.is_set()  # other schema's listener stays asleep


async def test_same_schema_instances_still_wake_each_other(pg_dsn, spec, event):
    a = PostgresCore(pg_dsn, schema="handler_x")
    b = PostgresCore(pg_dsn, schema="handler_x")
    try:
        await asyncio.gather(a.get_thread("x"), b.get_thread("x"))
        await asyncio.sleep(0.2)
        b._wake.clear()
        await a.put(event("wake"), spec)
        await asyncio.wait_for(b.wait_for_work(5), 6)
        assert b._wake.is_set() is False  # consumed by wait_for_work
        assert len(await b.claim("w", Lane.INTERACTIVE, 5, 60)) == 1
    finally:
        await asyncio.gather(a.close(), b.close())


async def test_default_has_no_schema_and_rejects_bad_names(pg_dsn):
    for bad in ("Robert; DROP", "1abc", "a-b", "x" * 41, ""):
        with pytest.raises(ValueError):
            PostgresCore(pg_dsn, schema=bad)
    core = PostgresCore(pg_dsn)
    try:
        await core.get_thread("x")
        assert core._schema is None
    finally:
        await core.close()


async def test_schema_named_with_sql_keyword(pg_dsn, spec, event):
    core = PostgresCore(pg_dsn, schema="select")
    try:
        await core.put(event("keyword-schema"), spec)
        assert await core.get_thread(spec.key) is not None
        assert core._listening.is_set()
    finally:
        await core.close()
