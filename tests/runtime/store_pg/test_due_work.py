"""Due work on Postgres: handler_control.due_work across tenants, claims and leases."""
import asyncio
from datetime import timedelta

import psycopg
import pytest

from handler.runtime.integrations.base import TimerKind
from handler.runtime.retention import retention_cleanup
from handler.runtime.scheduler import Scheduler
from handler.runtime.store.postgres import PostgresStore
from handler.runtime.store.postgres_hub import PostgresHub
from handler.runtime.timer import HostTimer, TenantDirectory, TimerTarget
from handler.runtime.types import (
    Approval, ApprovalStatus, Event, EventStatus, Lane, Schedule, ThreadSpec, ThreadType, utcnow,
)


@pytest.fixture
async def hub(pg_dsn):
    shared = PostgresHub(pg_dsn, max_size=4)
    try:
        yield shared
    finally:
        await shared.close()


@pytest.fixture
async def tenants(pg_dsn, hub):
    a = PostgresStore(pg_dsn, schema="handler_a", hub=hub)
    b = PostgresStore(pg_dsn, schema="handler_b", hub=hub)
    try:
        yield a, b
    finally:
        await asyncio.gather(a.close(), b.close())


def rows(dsn, query, *params):
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()


def target(store, kinds, context=None):
    found = TimerTarget(store.due_tenant, store.due_tenant, store.due_queue())
    for kind in kinds:
        found.bind(kind, context)
    return found


async def test_rows_live_in_the_control_schema_keyed_by_tenant_schema(tenants, pg_dsn):
    a, b = tenants
    await a.create_schedule(Schedule("s", "p", "every 1h", next_run_at=utcnow()))
    await b.set_due("test.kind", "x", utcnow())
    assert sorted(rows(pg_dsn, "SELECT tenant, kind FROM handler_control.due_work")) == [
        ("handler_a", "schedule.fire"), ("handler_b", "test.kind")]
    assert a.due_queue() is b.due_queue()  # one queue for the host's database


async def test_the_timer_claims_only_tenants_it_serves_and_runs_each_in_its_own(tenants):
    a, b = tenants
    seen = []

    async def handler(context, item):
        seen.append((context, item.tenant, item.ref))
        return None

    for store in tenants:
        await store.set_due("test.kind", store.due_tenant, utcnow())
    await a.set_due("test.kind", "later", utcnow() + timedelta(hours=1))
    directory = TenantDirectory()
    directory.register(target(a, [TimerKind("test.kind", handler)], "ctx-a"))
    timer = HostTimer(directory)
    assert await timer.run_due() == 1
    assert seen == [("ctx-a", "handler_a", "handler_a")]
    assert [r["ref"] for r in await b.due_rows()] == ["handler_b"]  # never claimed
    directory.register(target(b, [TimerKind("test.kind", handler)], "ctx-b"))
    await timer.run_due()
    assert seen[-1] == ("ctx-b", "handler_b", "handler_b")
    assert (await timer._next_due()) > utcnow() + timedelta(minutes=59)


async def test_two_timers_never_claim_the_same_row(tenants):
    a, _ = tenants
    for i in range(20):
        await a.set_due("test.kind", str(i), utcnow())
    queue = a.due_queue()
    now = utcnow()
    first, second = await asyncio.gather(
        queue.claim("one", ["handler_a"], now, 15, 60),
        queue.claim("two", ["handler_a"], now, 15, 60))
    assert len(first) + len(second) == 20
    assert not {i.ref for i in first} & {i.ref for i in second}
    assert await queue.claim("three", ["handler_a"], now, 50, 60) == []  # all leased
    await queue.release("one")
    assert len(await queue.claim("three", ["handler_a"], now, 50, 60)) == len(first)


async def test_a_lapsed_lease_is_claimed_again_and_the_old_owner_cannot_settle(tenants):
    a, _ = tenants
    await a.set_due("test.kind", "x", utcnow())
    queue = a.due_queue()
    item, = await queue.claim("dead", ["handler_a"], utcnow(), 5, 0.01)
    await asyncio.sleep(0.05)
    again, = await queue.claim("alive", ["handler_a"], utcnow(), 5, 60)
    await queue.finish(item, "dead", None, utcnow())  # fenced: not its lease any more
    assert len(await a.due_rows()) == 1
    await queue.finish(again, "alive", None, utcnow())
    assert await a.due_rows() == []


async def test_schedules_fire_through_the_timer_on_postgres(tenants):
    a, _ = tenants
    schedule = Schedule("s", "p", "every 30m", next_run_at=utcnow() - timedelta(days=1))
    await a.create_schedule(schedule)
    scheduler = Scheduler(a, default_harness="fake")
    directory = TenantDirectory()
    directory.register(target(a, [TimerKind("schedule.fire",
                                            lambda _c, i: scheduler.fire_due(i.ref))]))
    assert await HostTimer(directory).run_due() == 1
    assert len(await a.pending_events(f"automation:{schedule.id}")) == 1
    row, = await a.due_rows()
    stored = await a.get_schedule(schedule.id)
    assert row["due_at"] == stored.next_run_at > utcnow() and row["version"] == 2


async def test_expiry_rows_and_backfill_on_postgres(tenants):
    a, _ = tenants
    approval = Approval(run_id="r", thread_key="chat:u:1", tool="t", action="a", args={},
                        requested_by="u", reason="", expires_at=utcnow() + timedelta(days=1))
    await a.create_approval(approval)
    assert [(r["kind"], r["ref"]) for r in await a.due_rows()] == [("approval.expire", approval.id)]
    async with a._conn() as conn:  # as written by a release without due_work
        await conn.execute("DELETE FROM handler_control.due_work")
    await a.create_schedule(Schedule("s", "p", "every 1h", next_run_at=utcnow()))
    async with a._conn() as conn:
        await conn.execute("DELETE FROM handler_control.due_work WHERE kind = 'schedule.fire'")
    await a.backfill_due_work()
    await a.backfill_due_work()
    assert sorted(r["kind"] for r in await a.due_rows()) == [
        "approval.expire", "retention.cleanup", "schedule.fire"]
    await a.transition_approval(approval.id, ApprovalStatus.PENDING, ApprovalStatus.DENIED,
                                decided_by="u")
    assert "approval.expire" not in {r["kind"] for r in await a.due_rows()}


async def test_retention_deletes_only_what_the_decision_covers(tenants):
    a, _ = tenants
    old = utcnow() - timedelta(days=91)
    spec = ThreadSpec("chat:u:1", ThreadType.CHAT, Lane.INTERACTIVE, owner_user_id="u")
    for key, kind in (("chat-old", "chat.message"), ("mail-old", "email.received"),
                      ("chat-new", "chat.message")):
        await a.put(Event(type=kind, source="t", payload={}, idempotency_key=key,
                          thread_key=spec.key, received_at=old if "old" in key else utcnow()),
                    spec)
    async with a._conn() as conn:
        await conn.execute("UPDATE events SET status = %s", (EventStatus.OVER_LIMIT.value,))
        await conn.execute(
            "INSERT INTO run_failures (run_id, thread_key, thread_type, attempts, event_ids, "
            "consumed_by, failed_at) VALUES ('r-old', 'k', 'chat', 3, '[]', 'r-old', %s), "
            "('r-new', 'k', 'chat', 3, '[]', 'r-new', now())", (old,))
        await conn.execute(
            "INSERT INTO deliveries (sink, key, payload, status, next_attempt_at, created_at, "
            "updated_at) VALUES ('s', 'dead-old', '{}', 'dead', now(), %s, %s), "
            "('s', 'dead-new', '{}', 'dead', now(), now(), now()), "
            "('s', 'pending-old', '{}', 'pending', now(), %s, %s)", (old, old, old, old))
    counts = await retention_cleanup(a)
    assert counts == {"run_failures": 1, "dead_deliveries": 1, "over_limit_events": 1}
    async with a._conn() as conn:
        keys = {r["idempotency_key"] for r in await (await conn.execute(
            "SELECT idempotency_key FROM events")).fetchall()}
        deliveries = {r["key"] for r in await (await conn.execute(
            "SELECT key FROM deliveries")).fetchall()}
    assert keys == {"mail-old", "chat-new"}  # an email record is never removed
    assert deliveries == {"dead-new", "pending-old"}


async def test_cancel_flag_is_stored_only_on_a_running_run(tenants):
    from handler.runtime.types import Run, RunStatus

    a, _ = tenants
    spec = ThreadSpec("chat:u:1", ThreadType.CHAT, Lane.INTERACTIVE, owner_user_id="u")
    await a.put(Event(type="chat.message", source="t", payload={}, idempotency_key="c1",
                      thread_key=spec.key), spec)
    lease, = await a.claim("w", Lane.INTERACTIVE, 1, 60)
    run = Run(id="run_1", thread_key=spec.key, lease_version=lease.version,
              trigger_event_ids=[], status=RunStatus.RUNNING, harness="fake", model=None)
    assert await a.create_run(lease, run)
    assert not await a.run_cancel_requested("run_1")
    assert await a.request_run_cancel("run_1", "admin")
    assert await a.run_cancel_requested("run_1")
    run.status = RunStatus.CANCELLED
    assert await a.finish_run(lease, run)
    assert not await a.request_run_cancel("run_1", "admin")
