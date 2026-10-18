"""Phase 2 domain behaviors, run against SQLiteStore and PostgresStore."""

import asyncio
from dataclasses import replace
from datetime import timedelta, timezone

from handler.runtime.store.postgres import PostgresStore
from handler.runtime.types import (
    Approval, ApprovalStatus, AuditEntry, Connection, Decision, Event, Lane,
    MemoryTopic, Schedule, ThreadSpec, ThreadType,
)


def firing(schedule, clock):
    key = f"automation:{schedule.id}"
    event = Event(
        type="automation.fired", source="scheduler", payload={"schedule_id": schedule.id},
        idempotency_key=f"automation:{schedule.id}:{schedule.next_run_at.isoformat()}",
        thread_key=key, received_at=clock(),
    )
    thread = ThreadSpec(key, ThreadType.AUTOMATION, Lane.BACKGROUND, owner_user_id="u")
    return event, thread


# --- schedules ---------------------------------------------------------------
async def test_schedule_crud_and_due(domain_store, clock):
    store = domain_store
    assert await store.next_due_at() is None
    schedule = Schedule(
        "Digest", "Summarize", "cron: 0 7 * * *", created_by="u", timezone="America/New_York",
        notify=["u", "你好"], harness="test", model="m", parent_thread_key="chat:u:1",
        next_run_at=clock(), last_run_at=clock() - timedelta(days=1), created_at=clock(),
    )
    await store.create_schedule(schedule)
    assert await store.get_schedule(schedule.id) == schedule
    assert await store.get_schedule("missing") is None
    old = Schedule("old", "", "every 1h", next_run_at=(clock() - timedelta(hours=1)).astimezone(
        timezone(timedelta(hours=3))), created_at=clock())
    future = Schedule("future", "", "every 1h", next_run_at=clock() + timedelta(hours=1),
                      created_at=clock())
    off = Schedule("off", "", "every 1h", next_run_at=clock() - timedelta(days=1),
                   enabled=False, created_at=clock())
    for item in (old, future, off):
        await store.create_schedule(item)
    assert [s.name for s in await store.due_schedules(clock())] == ["old", "Digest"]
    assert [s.name for s in await store.due_schedules(clock(), limit=1)] == ["old"]
    assert await store.next_due_at() == old.next_run_at
    assert len(await store.list_schedules()) == 3
    assert len(await store.list_schedules(include_disabled=True)) == 4
    changed = replace(schedule, name="Renamed", enabled=False, notify=[], next_run_at=None)
    await store.update_schedule(changed)
    assert await store.get_schedule(schedule.id) == changed
    assert await store.delete_schedule(schedule.id)
    assert not await store.delete_schedule(schedule.id)


async def test_fire_schedule_cas_routes_and_advances(domain_store, clock):
    store = domain_store
    schedule = Schedule("s", "p", "every 1h", created_by="u", next_run_at=clock(),
                        created_at=clock())
    await store.create_schedule(schedule)
    event, thread = firing(schedule, clock)
    later = clock() + timedelta(hours=1)
    results = await asyncio.gather(*[
        store.fire_schedule(schedule, schedule.next_run_at, later, event, thread)
        for _ in range(4)
    ])
    assert sum(results) == 1
    stored = await store.get_schedule(schedule.id)
    assert stored.next_run_at == later and stored.last_run_at == clock() and stored.enabled
    assert (await store.get_thread(thread.key)).type is ThreadType.AUTOMATION
    assert [e.id for e in await store.pending_events(thread.key)] == [event.id]
    # stale expected value loses
    assert not await store.fire_schedule(schedule, schedule.next_run_at, later, event, thread)


async def test_fire_schedule_one_shot_disables_and_event_idempotent(domain_store, clock):
    store = domain_store
    schedule = Schedule("once", "p", "at: x", one_shot=True, next_run_at=clock(),
                        created_at=clock())
    await store.create_schedule(schedule)
    event, thread = firing(schedule, clock)
    assert await store.put(event, thread)  # event already exists
    assert await store.fire_schedule(schedule, schedule.next_run_at, None, event, thread)
    stored = await store.get_schedule(schedule.id)
    assert stored.enabled is False and stored.next_run_at is None
    assert await store.next_due_at() is None
    assert len(await store.pending_events(thread.key)) == 1


# --- approvals and audit -------------------------------------------------------
def approval(**kw):
    return Approval(run_id="r", thread_key="t", tool="x", action="a.x",
                    args={"k": [1, {"z": None}]}, requested_by="u", **kw)


async def test_approval_roundtrip_and_cas(domain_store, clock):
    store = domain_store
    item = approval(expires_at=clock() + timedelta(days=1))
    await store.create_approval(item)
    assert await store.get_approval(item.id) == item
    assert await store.get_approval("missing") is None
    assert not await store.transition_approval(
        item.id, ApprovalStatus.APPROVED, ApprovalStatus.EXECUTED)
    results = await asyncio.gather(*[
        store.transition_approval(item.id, ApprovalStatus.PENDING,
                                  ApprovalStatus.APPROVED, decided_by=f"b{i}")
        for i in range(4)
    ])
    assert sum(results) == 1
    got = await store.get_approval(item.id)
    assert got.status is ApprovalStatus.APPROVED and got.decided_at == clock()
    result = {"content": {"a": 1}, "is_error": False}
    assert await store.transition_approval(
        item.id, ApprovalStatus.APPROVED, ApprovalStatus.EXECUTED, result=result)
    after = await store.get_approval(item.id)
    assert after.result == result and after.decided_by == got.decided_by


async def test_list_approvals_and_audit_order(domain_store):
    store = domain_store
    first, second = approval(), approval()
    await store.create_approval(first)
    await store.create_approval(second)
    await store.transition_approval(first.id, ApprovalStatus.PENDING,
                                    ApprovalStatus.DENIED, decided_by="b")
    assert [a.id for a in await store.list_approvals(ApprovalStatus.PENDING)] == [second.id]
    assert [a.id for a in await store.list_approvals()] == [second.id, first.id]
    assert len(await store.list_approvals(limit=1)) == 1
    for i, run in enumerate(["r1", "r2", "r1"]):
        await store.add_audit(AuditEntry(
            run_id=run, thread_key="t", actor=None, tool=f"t{i}", action="a.b",
            decision=Decision.ALLOW, status="ok"))
    assert [e.tool for e in await store.list_audit()] == ["t2", "t1", "t0"]
    entries = await store.list_audit("r1")
    assert [e.tool for e in entries] == ["t2", "t0"]
    assert entries[0].decision is Decision.ALLOW and entries[0].actor is None


# --- memory and settings -------------------------------------------------------
async def test_memory_version_conflicts(domain_store, clock):
    store = domain_store
    first = await store.put_topic(MemoryTopic("a", "A", "one"), None)
    assert first.version == 1 and first.updated_at == clock()
    assert await store.put_topic(MemoryTopic("a", "A", "lost"), None) is None
    results = await asyncio.gather(*[
        store.put_topic(MemoryTopic("a", "A", str(i)), 1) for i in range(4)
    ])
    assert sum(result is not None for result in results) == 1
    assert (await store.get_topic("a")).version == 2
    assert await store.put_topic(MemoryTopic("missing", "", ""), 1) is None
    results = await asyncio.gather(*[
        store.put_topic(MemoryTopic("b", "B", str(i)), None) for i in range(4)
    ])
    assert sum(result is not None for result in results) == 1
    assert [t.name for t in await store.list_topics()] == ["a", "b"]
    assert await store.delete_topic("a")
    assert not await store.delete_topic("a")
    assert await store.get_topic("a") is None


async def test_settings_history(domain_store, clock):
    store = domain_store
    assert await store.get_settings("persona") is None
    results = await asyncio.gather(*[
        store.put_settings("persona", {"system": str(i)}, updated_by="x", expected_version=None)
        for i in range(4)
    ])
    assert sum(result is not None for result in results) == 1
    first = await store.get_settings("persona")
    assert first.version == 1 and first.updated_at == clock() and first.updated_by == "x"
    assert await store.put_settings("persona", {}, updated_by=None, expected_version=None) is None
    assert await store.put_settings("persona", {}, updated_by=None, expected_version=5) is None
    second = await store.put_settings(
        "persona", {"system": "two", "n": [1, {"a": None}]}, updated_by="y",
        expected_version=1, schema_version=2,
    )
    assert second.version == 2 and second.schema_version == 2
    assert await store.get_settings("persona") == second
    history = await store.settings_history("persona")
    assert [v.version for v in history] == [2, 1] and history[1] == first
    assert len(await store.settings_history("persona", limit=1)) == 1
    assert await store.get_settings("limits") is None


# --- notifications and connections ---------------------------------------------
async def test_notifications(domain_store, clock):
    store = domain_store
    first = await store.add_notification("u", "chat:u:1", "run1", "hello 你好")
    second = await store.add_notification("u", "chat:u:1", None, "again")
    await store.add_notification("other", "t", None, "x")
    assert first.created_at == clock() and first.read_at is None
    assert [n.id for n in await store.list_notifications("u")] == [second.id, first.id]
    clock.advance(5)
    assert await store.mark_read(first.id)
    assert not await store.mark_read("missing")
    assert [n.id for n in await store.list_notifications("u", unread_only=True)] == [second.id]
    read = (await store.list_notifications("u"))[1]
    assert read.read_at == clock() and read.run_id == "run1" and read.message == "hello 你好"
    clock.advance(5)
    await store.mark_read(first.id)  # idempotent: keeps the first read time
    assert (await store.list_notifications("u"))[1].read_at == read.read_at


async def test_connections(domain_store, clock):
    store = domain_store
    assert await store.find_connection("google", "user:u") is None
    mine = Connection("google", "user:u", "platform", external_ref="acct", secret_ref="s1",
                      scopes=["mail", "cal"], created_by="u", created_at=clock())
    other = Connection("google", "user:v", "local", created_at=clock())
    shared = Connection("slack", "instance", "broker", created_at=clock())
    for item in (mine, other, shared):
        await store.put_connection(item)
    assert await store.get_connection(mine.id) == mine
    assert await store.get_connection("missing") is None
    assert await store.find_connection("google", "user:u") == mine
    assert await store.find_connection("google", "user:v") == other
    assert await store.find_connection("slack", "user:u") is None
    assert [c.id for c in await store.list_connections()] == [mine.id, other.id, shared.id]
    assert [c.id for c in await store.list_connections("user:u")] == [mine.id]
    clock.advance(1)
    flagged = replace(mine, status="needs_reauth", last_used_at=clock(), scopes=[])
    await store.put_connection(flagged)  # replace by id
    assert await store.get_connection(mine.id) == flagged
    assert await store.find_connection("google", "user:u") is None
    newer = Connection("google", "user:u", "platform", created_at=clock())
    await store.put_connection(newer)
    assert await store.find_connection("google", "user:u") == newer
    assert len(await store.list_connections()) == 4


async def test_delete_connection_and_secrets(domain_store, clock):
    store = domain_store
    connection = Connection("google", "instance", "local", secret_ref="connection:x",
                            created_at=clock())
    await store.put_connection(connection)
    assert await store.get_secret("connection:x") is None
    await store.put_secret("connection:x", "ciphertext-1")
    await store.put_secret("connection:x", "ciphertext-2")  # upsert
    assert await store.get_secret("connection:x") == "ciphertext-2"
    assert await store.delete_secret("connection:x")
    assert not await store.delete_secret("connection:x")
    assert await store.get_secret("connection:x") is None
    assert await store.delete_connection(connection.id)
    assert not await store.delete_connection(connection.id)
    assert await store.get_connection(connection.id) is None


# --- postgres only -------------------------------------------------------------
async def test_two_postgres_processes_fire_one_schedule_once(pg_dsn, clock):
    first, second = PostgresStore(pg_dsn, clock=clock), PostgresStore(pg_dsn, clock=clock)
    try:
        schedule = Schedule("s", "p", "every 1h", next_run_at=clock(), created_at=clock())
        await first.create_schedule(schedule)
        event, thread = firing(schedule, clock)
        later = clock() + timedelta(hours=1)
        results = await asyncio.gather(
            first.fire_schedule(schedule, schedule.next_run_at, later, event, thread),
            second.fire_schedule(schedule, schedule.next_run_at, later, event, thread),
        )
        assert sorted(results) == [False, True]
        assert len(await second.pending_events(thread.key)) == 1
    finally:
        await first.close()
        await second.close()


async def test_fire_schedule_wakes_other_process(pg_dsn, clock):
    first, second = PostgresStore(pg_dsn, clock=clock), PostgresStore(pg_dsn, clock=clock)
    try:
        await second.wait_for_work(0.01)  # ensure ready, consume initial wake
        await second.wait_for_work(0.01)
        schedule = Schedule("s", "p", "every 1h", next_run_at=clock(), created_at=clock())
        await first.create_schedule(schedule)
        event, thread = firing(schedule, clock)
        waiter = asyncio.create_task(second.wait_for_work(10))
        await asyncio.sleep(0.2)
        assert not waiter.done()
        assert await first.fire_schedule(schedule, schedule.next_run_at, None, event, thread)
        await asyncio.wait_for(waiter, 5)
    finally:
        await first.close()
        await second.close()
