"""Recovering memory.save calls that waited for approval (memory_recovery.py)."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

from handler.runtime.auth import Authenticator
from handler.runtime.context import RunContext
from handler.runtime.gateway.base import ToolResult, ToolSpec
from handler.runtime.gateway.gateway import Gateway
from handler.runtime.gateway.policy import GrantPolicy
from handler.runtime.memory import MemoryTools
from handler.runtime.memory_recovery import parse_statuses, recover_saves
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import ApprovalStatus, Risk
from handler.runtime.web.memory_recovery_api import memory_recovery_router


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
async def setup(tmp_path):
    clock = Clock()
    store = SQLiteStore(tmp_path / "h.db", clock=clock)
    emitted = []

    async def emit(event):
        emitted.append(event)

    # The old behaviour: memory.save asks (as it did on untrusted threads).
    gateway = Gateway(store, GrantPolicy({"actions": {"memory.save": "ask", "x.*": "ask"}}),
                      emit_event=emit, approval_ttl_seconds=3600, clock=clock)
    gateway.register(MemoryTools(store))

    async def other(ctx, args):
        return ToolResult("ran")

    gateway.register(ToolSpec("x_do", "x.do", Risk.WRITE_INTERNAL, "x",
                              {"type": "object", "properties": {}}, other))
    yield store, gateway, clock, emitted
    await store.close()


MAIL = RunContext("r1", "mail:mbx1:conv1")
EMAIL = RunContext("r2", "email:a@b.com:<m1>")


async def save(gateway, ctx, name, content):
    result = await gateway.call(ctx, "memory_save", {"name": name, "content": content,
                                                     "description": name})
    return result.content["approval_id"]


async def test_recover_pending_and_expired_in_order(setup):
    store, gateway, clock, emitted = setup
    first = await save(gateway, MAIL, "inquiries/acme", "first fact")
    clock.now += timedelta(minutes=1)
    second = await save(gateway, EMAIL, "inquiries/acme", "second fact")
    clock.now += timedelta(hours=2)
    assert await gateway.expire_stale() == 2  # both expired
    emitted.clear()
    third = await save(gateway, MAIL, "companies/acme", "pending fact")
    clock.now += timedelta(seconds=1)
    dup = await save(gateway, MAIL, "companies/acme", "pending fact")  # same content twice
    denied = await save(gateway, MAIL, "policies", "denied fact")
    await gateway.deny(denied, "owner", "no")
    other = (await gateway.call(MAIL, "x_do", {})).content["approval_id"]
    emitted.clear()

    dry = await recover_saves(gateway, store, decided_by="admin", dry_run=True)
    assert [(r["approval_id"], r["outcome"]) for r in dry] == [
        (first, "would_run"), (second, "would_run"), (third, "would_run"), (dup, "would_run")]
    assert await store.get_topic("inquiries/acme") is None

    results = await recover_saves(gateway, store, decided_by="admin")
    assert [(r["approval_id"], r["outcome"]) for r in results] == [
        (first, "executed"), (second, "executed"), (third, "executed"), (dup, "skipped")]
    assert results[3]["detail"] == "content already in the topic"
    topic = await store.get_topic("inquiries/acme")
    assert topic.content == "first fact\n\nsecond fact"  # oldest first
    assert topic.source_untrusted and topic.updated_thread_key == EMAIL.thread_key
    assert (await store.get_approval(first)).status is ApprovalStatus.EXECUTED
    assert (await store.get_approval(first)).decided_by == "admin"
    assert (await store.get_approval(dup)).status is ApprovalStatus.PENDING
    assert (await store.get_approval(denied)).status is ApprovalStatus.DENIED
    assert (await store.get_approval(other)).status is ApprovalStatus.PENDING
    assert emitted == []  # no thread was woken
    # Running again finds nothing new to do.
    again = await recover_saves(gateway, store, decided_by="admin")
    assert [r["outcome"] for r in again] == ["skipped"]

    # Denied ones only when asked for, by status or id.
    by_id = await recover_saves(gateway, store, decided_by="admin",
                                ids=[denied, other, "apr_missing", first])
    outcomes = {r["approval_id"]: r["outcome"] for r in by_id}
    assert outcomes == {denied: "executed", other: "not_found", "apr_missing": "not_found",
                        first: "skipped"}
    assert (await store.get_topic("policies")).content == "denied fact"


async def test_notify_threads_wakes_pending_only(setup):
    store, gateway, clock, emitted = setup
    pending = await save(gateway, MAIL, "a", "x")
    emitted.clear()
    results = await recover_saves(gateway, store, decided_by="admin", notify_threads=True)
    assert results[0]["outcome"] == "executed"
    assert [e.type for e in emitted] == ["approval.resolved"]
    assert emitted[0].payload["approval_id"] == pending


def test_parse_statuses():
    assert parse_statuses(None) == [ApprovalStatus.PENDING, ApprovalStatus.EXPIRED]
    assert parse_statuses("expired, denied") == [ApprovalStatus.EXPIRED, ApprovalStatus.DENIED]
    for bad in ("executed", "nope"):
        with pytest.raises(ValueError):
            parse_statuses(bad)


async def test_api(setup):
    store, gateway, clock, emitted = setup
    pending = await save(gateway, MAIL, "notes", "remember this")
    app = FastAPI()
    app.include_router(memory_recovery_router(gateway, store,
                                              Authenticator(store, "test-admin-key")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        assert (await client.get("/api/admin/memory-saves")).status_code == 401
        headers = {"Authorization": "Bearer test-admin-key"}
        listed = (await client.get("/api/admin/memory-saves", headers=headers)).json()
        assert [s["approval_id"] for s in listed["saves"]] == [pending]
        assert listed["saves"][0]["content_preview"] == "remember this"
        assert (await client.get("/api/admin/memory-saves", params={"status": "executed"},
                                 headers=headers)).status_code == 422
        dry = (await client.post("/api/admin/memory-saves/recover", json={"dry_run": True},
                                 headers=headers)).json()
        assert dry["counts"] == {"would_run": 1}
        done = (await client.post("/api/admin/memory-saves/recover", json={},
                                  headers=headers)).json()
        assert done["counts"] == {"executed": 1}
        assert done["results"][0]["status"] == "executed"
    assert (await store.get_topic("notes")).content == "remember this"
    assert (await store.get_approval(pending)).decided_by.endswith("(memory recovery)")


async def test_untrusted_save_runs_unasked_with_default_policy(tmp_path):
    store = SQLiteStore(tmp_path / "h.db")

    async def emit(event):
        pass

    gateway = Gateway(store, GrantPolicy(), emit_event=emit)
    gateway.register(MemoryTools(store))
    result = await gateway.call(MAIL, "memory_save", {"name": "a", "content": "x",
                                                      "description": "d"})
    assert not result.is_error and result.content["version"] == 1
    assert (await store.get_topic("a")).source_untrusted
    rewrite = await gateway.call(MAIL, "memory_rewrite", {"name": "a", "content": "y",
                                                          "description": "d"})
    assert rewrite.content["status"] == "pending_approval"
    await store.close()
