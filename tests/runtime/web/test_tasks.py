"""Tasks API: background runs with derived status, trigger summaries and the admin trace."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest

from handler.runtime.auth import Authenticator
from handler.runtime.bus import BusMessage, RunEventBus
from handler.runtime.run import RunConfig, RunExecutor
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.store.sqlite_users import User
from handler.runtime.tasks import publish_queued, publish_task, trigger_summary
from handler.runtime.types import (
    Approval,
    Event,
    Lane,
    Message,
    Role,
    Run,
    RunStatus,
    RunStep,
    StepKind,
    ThreadSpec,
    ThreadType,
    TokenUsage,
    new_id,
    utcnow,
)
from handler.runtime.web.app import create_app, sse_stream
from handler.runtime.web.tasks_api import tasks_router

EMAIL = {"subject": "Fwd: Quote request", "forwarder": {"name": "Danny", "address": "danny@crontal.ai"},
         "original": {"from": {"name": "Alice Buyer", "address": "alice@buyer.example"},
                      "subject": "Quote request"}}
AUTOMATION = {"schedule_id": "sch_1", "name": "Daily digest", "prompt": "Summarize"}
CRONTAL = {"id": "77", "type": "bid_submitted", "entity_type": "rfq", "entity_id": 42}


@pytest.fixture
async def db(tmp_path):
    store = SQLiteStore(tmp_path / "tasks.db")
    await store.create_user(User("member", "Member"))
    await store.create_user(User("boss", "Boss", "admin"))
    yield store
    await store.close()


async def queue(db, key, kind, event_type, payload, owner="member"):
    lane = Lane.INTERACTIVE if kind is ThreadType.CHAT else Lane.BACKGROUND
    await db.put(Event(type=event_type, source="test", payload=payload,
                       idempotency_key=new_id("idem"), thread_key=key, user_id=owner),
                 ThreadSpec(key=key, type=kind, lane=lane, owner_user_id=owner, harness="codex"))
    return lane


async def start(db, key, kind, event_type, payload, owner="member"):
    lane = await queue(db, key, kind, event_type, payload, owner=owner)
    lease, = await db.claim("worker", lane, 1, 60)
    events = await db.pending_events(key)
    run = Run(id=new_id("run"), thread_key=key, lease_version=lease.version,
              trigger_event_ids=[e.id for e in events], status=RunStatus.RUNNING,
              harness="codex", model="gpt-test")
    assert await db.create_run(lease, run)
    return lease, run


async def finish(db, lease, run, text="Done", cost=0.25):
    usage = TokenUsage(input_tokens=1200, output_tokens=300)
    await db.add_step(RunStep(run.id, 1, StepKind.LLM_CALL, "gpt-test", "stream",
                              model="gpt-test", usage=usage, cost_usd=cost))
    await db.add_step(RunStep(run.id, 2, StepKind.TOOL_CALL, "crontal_api_get", "stream",
                              data={"id": "c1", "input": json.dumps({"path": "/rfq/42"}),
                                    "output": "x" * 5000}))
    assert await db.add_messages(lease, [Message(run.thread_key, Role.ASSISTANT,
                                                  [{"type": "text", "text": text}], run_id=run.id)])
    run.status, run.usage, run.cost_usd, run.finished_at = RunStatus.COMPLETED, usage, cost, utcnow()
    assert await db.finish_run(lease, run)
    assert await db.complete(lease, run.id, run.trigger_event_ids)


async def fail(db, lease, run, retry):
    run.status, run.error, run.finished_at = RunStatus.FAILED, "HarnessError: boom", utcnow()
    assert await db.finish_run(lease, run)
    assert await db.fail(lease, run.error, retry)


@pytest.fixture
async def seeded(db):
    runs = {}
    lease, runs["email"] = await start(db, "email:quote", ThreadType.EMAIL, "email.received", EMAIL)
    await finish(db, lease, runs["email"], "I replied with our price list. " * 20)
    lease, runs["retry"] = await start(db, "automation:sch_1", ThreadType.AUTOMATION,
                                       "automation.fired", AUTOMATION)
    await fail(db, lease, runs["retry"], 300)
    lease, runs["dead"] = await start(db, "automation:sch_2", ThreadType.AUTOMATION,
                                      "automation.fired", {**AUTOMATION, "name": "Broken job"})
    await fail(db, lease, runs["dead"], None)
    lease, runs["approval"] = await start(db, "crontal:rfq:7", ThreadType.CRONTAL,
                                          "crontal.notification", {**CRONTAL, "entity_id": 7})
    await finish(db, lease, runs["approval"], "Waiting for approval to send.")
    await db.create_approval(Approval(run_id=runs["approval"].id, thread_key="crontal:rfq:7",
                                      tool="gmail_send", action="gmail.send",
                                      args={"to": "alice@buyer.example"}, requested_by="member"))
    lease, runs["chat"] = await start(db, "chat:member:c1", ThreadType.CHAT, "chat.message",
                                      {"text": "private"})
    await finish(db, lease, runs["chat"], "Private chat reply")
    _, runs["running"] = await start(db, "crontal:rfq:42", ThreadType.CRONTAL,
                                     "crontal.notification", CRONTAL)
    lease, runs["boss"] = await start(db, "crontal:rfq:boss", ThreadType.CRONTAL,
                                      "crontal.notification", CRONTAL, owner="boss")
    await finish(db, lease, runs["boss"])
    await db.create_approval(Approval(run_id=runs["boss"].id,
                                      thread_key=runs["boss"].thread_key,
                                      tool="gmail_send", action="gmail.send",
                                      args={"to": "alice@buyer.example"}, requested_by="boss"))
    await queue(db, "email:new", ThreadType.EMAIL, "email.received",
                {"subject": "Hello", "forwarder": {"name": "", "address": "bob@example.com"}})
    return runs


@pytest.fixture
async def clients(db):
    _, member = await db.create_token("member")
    _, boss = await db.create_token("boss")
    app = create_app(db, RunEventBus(), Authenticator(db, "admin-key"))
    app.include_router(tasks_router(db, RunEventBus(), Authenticator(db, "admin-key")))
    transport = httpx.ASGITransport(app=app)
    made = []
    for token in (member, boss, "admin-key"):
        made.append(httpx.AsyncClient(transport=transport, base_url="http://t",
                                      headers={"Authorization": f"Bearer {token}"}))
    yield made
    for c in made:
        await c.aclose()


async def test_lists_non_chat_tasks_with_derived_status_and_summaries(seeded, clients):
    member, _, _ = clients
    r = await member.get("/api/tasks")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    by_run = {i["run_id"]: i for i in items}
    assert seeded["chat"].id not in by_run, "chat runs are not tasks"
    assert items[0]["status"] == "queued" and items[0]["run_id"] is None
    queued = [i for i in items if i["status"] == "queued"]
    assert {i["thread_key"] for i in queued} == {"email:new", "automation:sch_1"}
    new_mail = next(i for i in queued if i["thread_key"] == "email:new")
    assert new_mail["trigger_summary"] == "Email from bob@example.com: Hello"
    retry = next(i for i in queued if i["thread_key"] == "automation:sch_1")
    assert retry["retry_at"] and retry["attempts"] == 1 and "boom" in retry["error"]

    email = by_run[seeded["email"].id]
    assert email["status"] == "succeeded"
    assert email["trigger_summary"] == "Email from Alice Buyer <alice@buyer.example>: Quote request"
    assert email["thread_type"] == "email" and email["thread_title"] == "Fwd: Quote request"
    assert email["summary"].startswith("I replied") and len(email["summary"]) <= 300
    assert email["cost_usd"] == 0.25 and email["usage"]["total_tokens"] == 1500
    assert email["duration_ms"] is not None and email["finished_at"]
    assert "steps" not in email
    assert by_run[seeded["retry"].id]["status"] == "failed"
    assert by_run[seeded["retry"].id]["trigger_summary"] == "Daily digest"
    assert by_run[seeded["dead"].id]["status"] == "dead"
    waiting = by_run[seeded["approval"].id]
    assert waiting["status"] == "waiting_approval" and len(waiting["pending_approval_ids"]) == 1
    running = by_run[seeded["running"].id]
    assert running["status"] == "running" and running["finished_at"] is None
    assert running["trigger_summary"] == "rfq 42 · bid_submitted"
    starts = [i["started_at"] for i in items if i["run_id"]]
    assert starts == sorted(starts, reverse=True), "newest first"


async def test_filters_and_validation(seeded, clients):
    member, _, _ = clients
    items = (await member.get("/api/tasks?status=failed,dead")).json()["items"]
    assert {i["run_id"] for i in items} == {seeded["retry"].id, seeded["dead"].id}
    items = (await member.get("/api/tasks?type=email")).json()["items"]
    assert {i["thread_key"] for i in items} == {"email:quote", "email:new"}
    items = (await member.get("/api/tasks?status=queued")).json()["items"]
    assert {i["status"] for i in items} == {"queued"}
    items = (await member.get("/api/tasks?status=running")).json()["items"]
    assert [i["run_id"] for i in items] == [seeded["running"].id]
    future = (utcnow() + timedelta(hours=1)).isoformat()
    assert (await member.get("/api/tasks", params={"since": future})).json()["items"] == []
    for bad in ("status=bogus", "type=chat", "cursor=%%%", "since=2026-10-18T00:00:00"):
        assert (await member.get(f"/api/tasks?{bad}")).status_code == 422, bad


async def test_cursor_pagination_is_stable_and_queued_only_on_first_page(seeded, clients):
    member, _, _ = clients
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await member.get("/api/tasks", params=params)).json()
        pages += 1
        runs = [i for i in page["items"] if i["run_id"]]
        assert len(runs) <= 2
        if pages > 1:
            assert all(i["status"] != "queued" for i in page["items"])
        seen += [i["run_id"] for i in runs]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 5
    assert pages == 3


async def test_detail_steps_are_admin_only(db, seeded, clients):
    member, boss, admin_key = clients
    run_id = seeded["email"].id
    plain = (await member.get(f"/api/tasks/{run_id}")).json()
    assert plain["run_id"] == run_id and "steps" not in plain
    assert (await boss.get(f"/api/tasks/{run_id}")).status_code == 404
    assert (await admin_key.get(f"/api/tasks/{run_id}")).status_code == 404

    # The admin user gets traces for their own tasks, including through an
    # explicit admin-key act-as request.
    owned = seeded["boss"]
    for client, headers in ((boss, {}), (admin_key, {"X-Handler-User": "boss"})):
        detail = (await client.get(f"/api/tasks/{owned.id}", headers=headers)).json()
        steps = [s for s in detail["steps"] if s["kind"] != "approval"]
        assert [s["kind"] for s in steps] == ["llm_call", "tool_call"]
        llm, tool = steps
        assert llm["model"] == "gpt-test" and llm["usage"]["input_tokens"] == 1200
        assert llm["cost_usd"] == 0.25 and "latency_ms" in llm
        assert tool["data"]["input"] == '{"path": "/rfq/42"}'
        assert tool["data"]["output"].endswith("[5000 chars]") and len(tool["data"]["output"]) < 2100
    trace = (await boss.get(f"/api/tasks/{owned.id}")).json()["steps"]
    approval = [s for s in trace if s["kind"] == "approval"]
    assert approval[0]["status"] == "pending" and approval[0]["data"]["args"]["to"] == "alice@buyer.example"
    # Chat runs are not tasks, even for admins; unknown runs 404 too.
    assert (await boss.get(f"/api/tasks/{seeded['chat'].id}")).status_code == 404
    assert (await member.get("/api/tasks/run_missing")).status_code == 404


async def test_tasks_require_authentication(seeded, clients):
    member, _, _ = clients
    async with httpx.AsyncClient(transport=member._transport, base_url="http://t") as anon:
        assert (await anon.get("/api/tasks")).status_code == 401
        assert (await anon.get(f"/api/tasks/{seeded['email'].id}")).status_code == 401


async def test_abandoned_running_run_reports_failed(db):
    lease, run = await start(db, "automation:lost", ThreadType.AUTOMATION, "automation.fired", AUTOMATION)
    # The worker's lease expires without the run row being finished.
    await db._call(lambda conn: conn.execute(
        "UPDATE threads SET lease_until = ? WHERE key = ?",
        ((utcnow() - timedelta(seconds=5)).isoformat(), "automation:lost")))
    rows = (await db.task_rows(run_id=run.id))[0]
    assert rows["task_status"] == "failed"
    from handler.runtime.tasks import get_task
    item = await get_task(db, run.id, user_id=None, include_steps=False)
    assert item["status"] == "failed" and "interrupted" in item["error"]


@pytest.mark.parametrize("event,summary", [
    ({"type": "email.received", "payload": {"subject": "S", "originals": [
        {"from": {"name": "", "address": "o@x.io"}, "subject": "Orig"}]}}, "Email from o@x.io: Orig"),
    ({"type": "email.received", "payload": {}}, "Email from unknown sender: (no subject)"),
    ({"type": "automation.fired", "payload": {"name": "Digest", "manual": True}}, "Digest (run manually)"),
    ({"type": "crontal.notification", "payload": {"type": "rfq_published", "entity_type": "rfq",
                                                    "entity_id": "9"}}, "rfq 9 · rfq_published"),
    ({"type": "approval.resolved", "payload": {"status": "approved", "tool": "gmail_send"}},
     "Approval approved: gmail_send"),
    ({"type": "gmail.message", "source": "gmail", "payload": {"subject": "Invoice"}}, "gmail.message: Invoice"),
    ({"type": "custom.thing", "source": "x", "payload": {}}, "custom.thing from x"),
])
def test_trigger_summaries_per_type(event, summary):
    assert trigger_summary([event]) == summary
    assert trigger_summary([event, event]) == summary + " (+1 more)"
    assert trigger_summary([]) == "No trigger recorded"


async def test_live_updates_on_the_bus_and_task_stream(db):
    bus = RunEventBus()
    db.on_routed = lambda event, spec: publish_queued(bus, event, spec)
    async with bus.subscribe("*") as q:
        await queue(db, "chat:member:x", ThreadType.CHAT, "chat.message", {"text": "hi"})
        assert q.empty(), "chat work is not a task"
        await queue(db, "email:live", ThreadType.EMAIL, "email.received", EMAIL)
        msg = q.get_nowait()
        assert msg.type == "task_updated" and msg.data["status"] == "queued"
        lease, = await db.claim("w", Lane.BACKGROUND, 1, 60)
        run = Run(id=new_id("run"), thread_key="email:live", lease_version=lease.version,
                  trigger_event_ids=[], status=RunStatus.RUNNING, harness="codex", model=None)
        await db.create_run(lease, run)
        await publish_task(bus, db, "email:live", run.id)
        msg = q.get_nowait()
        assert msg.data["status"] == "running" and msg.data["run_id"] == run.id
        assert "steps" not in msg.data

    gen = sse_stream(bus, "*", heartbeat_seconds=5, accept=lambda m: m.type == "task_updated")
    assert await gen.__anext__() == ": connected\n\n"
    bus.publish(BusMessage("email:live", "r", "run_event", {"kind": "assistant_text"}))
    bus.publish(BusMessage("email:live", "r", "task_updated", {"status": "succeeded"}))
    frame = await asyncio.wait_for(gen.__anext__(), 1)
    assert json.loads(frame[6:])["data"]["status"] == "succeeded"
    await gen.aclose()


class _Harness:
    name = "codex"

    async def run(self, req):
        from handler.runtime.harness.base import RunEvent, RunEventKind
        yield RunEvent(RunEventKind.ASSISTANT_TEXT, {"text": "Filed the RFQ"})
        yield RunEvent(RunEventKind.FINAL, {"text": "Filed the RFQ"})


async def test_executor_publishes_task_status_for_background_runs(db, tmp_path):
    from handler.runtime.observability.pricing import PriceTable
    bus = RunEventBus()
    executor = RunExecutor(db, {"codex": _Harness()}, bus, RunConfig(data_dir=tmp_path),
                           PriceTable([]))
    async with bus.subscribe("*") as q:
        for key, kind, typ, payload in (("crontal:rfq:1", ThreadType.CRONTAL,
                                         "crontal.notification", CRONTAL),
                                        ("chat:member:z", ThreadType.CHAT, "chat.message",
                                         {"text": "hi"})):
            lane = await queue(db, key, kind, typ, payload)
            lease, = await db.claim("w", lane, 1, 60)
            await executor.execute(lease)
        updates = []
        while not q.empty():
            msg = q.get_nowait()
            if msg.type == "task_updated":
                updates.append(msg)
    assert [m.data["status"] for m in updates] == ["running", "succeeded"]
    assert {m.thread_key for m in updates} == {"crontal:rfq:1"}
    assert updates[-1].data["summary"] == "Filed the RFQ"
