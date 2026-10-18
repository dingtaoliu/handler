from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from handler.runtime.auth import Authenticator
from handler.runtime.bus import BusMessage, RunEventBus
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.store.sqlite_users import User
from handler.runtime.types import (
    Event,
    Lane,
    Message,
    Role,
    Run,
    RunStatus,
    RunStep,
    StepKind,
    Thread,
    ThreadSpec,
    ThreadType,
    TokenUsage,
    utcnow,
)
from handler.runtime.web.app import create_app, sse_stream


class FakeStore:
    def __init__(self) -> None:
        self.events: dict[str, tuple[Event, ThreadSpec | None]] = {}
        self.threads: dict[str, Thread] = {}
        self.runs: dict[str, Run] = {}
        self.messages: list[Message] = []
        self.steps: list[RunStep] = []

    async def put(self, event, thread):
        if any(e.idempotency_key == event.idempotency_key
               for e, _ in self.events.values()):
            return False
        self.events[event.id] = (event, thread)
        if thread and thread.key not in self.threads:
            self.threads[thread.key] = Thread(
                key=thread.key, type=thread.type, lane=thread.lane,
                owner_user_id=thread.owner_user_id, harness=thread.harness,
                model=thread.model, priority=thread.priority, session_id=None,
                pending_since=utcnow(), available_at=None, attempts=0,
                last_error=None, dead=False, lease_owner=None, lease_until=None,
                lease_version=0, summary=None, created_at=utcnow(),
            )
        return True

    async def list_threads(self, owner_user_id=None, limit=100):
        return [t for t in self.threads.values()
                if owner_user_id in (None, t.owner_user_id)]

    async def thread_metadata(self, keys, user_id):
        return {}

    async def get_thread(self, key):
        return self.threads.get(key)

    async def pending_events(self, key):
        return [e for e, _ in self.events.values() if e.thread_key == key]

    async def get_run(self, run_id):
        return self.runs.get(run_id)

    async def list_runs(self, key, limit=50):
        return [r for r in self.runs.values() if r.thread_key == key]

    async def list_messages(self, key, limit=200, before=None, *, newest=False):
        rows = [m for m in self.messages if m.thread_key == key]
        return rows[-limit:] if newest else rows[:limit]

    async def list_steps(self, run_id):
        return [s for s in self.steps if s.run_id == run_id]


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def bus():
    return RunEventBus()


@pytest.fixture
async def users(tmp_path):
    db = SQLiteStore(tmp_path / "users.db")
    await db.create_user(User("u1", "User One"))
    yield db
    await db.close()


@pytest.fixture
async def client(store, bus, users):
    _, token = await users.create_token("u1")
    app = create_app(store, bus, Authenticator(users, "admin-key-for-tests"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t",
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        yield c


async def test_chat_creates_event_and_thread(client, store, bus):
    async with bus.subscribe("*") as q:
        r = await client.post(
            "/api/chat",
            json={"text": "hi", "conversation_id": "c1",
                  "client_message_id": "m1"},
        )
        msg = q.get_nowait()
    body = r.json()
    assert body["thread_key"] == "chat:u1:c1"
    assert body["conversation_id"] == "c1"
    assert body["duplicate"] is False
    event, spec = store.events[body["event_id"]]
    assert event.type == "chat.message" and event.source == "web"
    assert event.payload == {"text": "hi"}
    assert event.idempotency_key == "u1:m1" and event.user_id == "u1"
    assert spec == ThreadSpec("chat:u1:c1", ThreadType.CHAT, Lane.INTERACTIVE,
                              owner_user_id="u1", harness="codex")
    assert msg.type == "event_queued" and msg.thread_key == "chat:u1:c1"


async def test_chat_defaults_and_duplicate(client):
    r1 = (await client.post("/api/chat", json={"text": "a"})).json()
    assert r1["conversation_id"]
    payload = {"text": "a", "conversation_id": "c",
               "client_message_id": "same"}
    assert (await client.post("/api/chat", json=payload)).json()["duplicate"] is False
    assert (await client.post("/api/chat", json=payload)).json()["duplicate"] is True


async def test_listing_endpoints(client, store):
    key = "chat:u1:c1"
    await client.post("/api/chat", json={"text": "x",
                                         "conversation_id": "c1"})
    store.messages.append(Message(key, Role.USER, [{"type": "text", "text": "x"}]))
    run = Run("run_1", key, 1, ["e"], RunStatus.COMPLETED, "codex", "m",
              usage=TokenUsage(input_tokens=5), cost_usd=0.01)
    store.runs[run.id] = run
    store.steps.append(RunStep("run_1", 1, StepKind.TOOL_CALL, "shell", "stream"))

    threads = (await client.get("/api/threads", params={"user_id": "u1"})).json()
    assert threads[0]["key"] == key and threads[0]["type"] == "chat"
    assert (await client.get("/api/threads", params={"user_id": "zz"})).json() == []

    t = (await client.get(f"/api/threads/{key}")).json()
    assert t["thread"]["lane"] == "interactive"
    assert len(t["pending_events"]) == 1

    msgs = (await client.get(f"/api/threads/{key}/messages")).json()
    assert msgs[0]["role"] == "user" and "T" in msgs[0]["created_at"]

    runs = (await client.get(f"/api/threads/{key}/runs")).json()
    assert runs[0]["status"] == "completed" and runs[0]["usage"]["input_tokens"] == 5

    detail = (await client.get("/api/runs/run_1")).json()
    assert detail["run"]["id"] == "run_1"
    assert detail["steps"][0]["kind"] == "tool_call"

    assert (await client.get("/api/runs/nope")).status_code == 404
    assert (await client.get("/api/threads/chat:x:y")).status_code == 404


async def test_sse_delivers_and_heartbeats(bus):
    gen = sse_stream(bus, "chat:u:c", heartbeat_seconds=0.05)
    assert await gen.__anext__() == ": connected\n\n"
    assert await gen.__anext__() == ": heartbeat\n\n"
    bus.publish(BusMessage("chat:u:c", "run_1", "run_started", {"a": 1}))
    frame = await asyncio.wait_for(gen.__anext__(), 1)
    assert frame.startswith("data: ")
    data = json.loads(frame[6:])
    assert data["type"] == "run_started" and data["run_id"] == "run_1"
    assert data["data"] == {"a": 1}
    await gen.aclose()


async def test_sse_route_registered_before_thread_route(client, store):
    # Routing sanity: the /stream suffix must not be swallowed by get_thread.
    routes = [r.path for r in client._transport.app.routes]  # type: ignore[attr-defined]
    assert routes.index("/api/threads/{key:path}/stream") < routes.index(
        "/api/threads/{key:path}")


async def test_timeline_html(client):
    r = await client.get("/timeline")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "prefers-color-scheme" in r.text and "EventSource" in r.text


async def test_messages_fill_in_tool_arguments_of_older_threads(client, store):
    await client.post("/api/chat", json={"text": "x", "conversation_id": "c1"})
    key = next(iter(store.threads))

    def tool_message(call_id, name, raw, input=None):
        return Message(key, Role.TOOL, [
            {"type": "tool_call", "id": call_id, "name": name, "kind": "k", "input": input},
            {"type": "tool_result", "id": call_id, "output": "ok", "is_error": False}],
            run_id="run_1", raw=raw)

    store.messages += [
        # Native harness, before arguments were carried: only the step has them.
        tool_message("c1", "crontal_get_rfq", {"type": "function_call_output", "call_id": "c1"}),
        tool_message("c2", "list_files", {"type": "function_call_output", "call_id": "c2"}),
        # Codex keeps the completed item, arguments included, in raw.
        tool_message("i1", "gateway/lookup", {"type": "item.completed", "item": {
            "id": "i1", "type": "mcp_tool_call", "server": "gateway", "tool": "lookup",
            "arguments": {"q": "x"}, "result": {"ok": True}, "status": "completed"}}),
        tool_message("i2", "shell", {"type": "item.completed", "item": {
            "id": "i2", "type": "command_execution", "command": "ls -la",
            "aggregated_output": "ok", "exit_code": 0, "status": "completed"}}),
        # Already filled in: left alone.
        tool_message("c3", "read_file", {}, input={"path": "a.txt"}),
    ]
    store.steps += [
        RunStep("run_1", 1, StepKind.TOOL_CALL, "crontal_get_rfq", "stream",
                data={"id": "c1", "kind": "function_call", "input": '{"rfq_id": 7}'}),
        RunStep("run_1", 2, StepKind.TOOL_CALL, "list_files", "stream",
                data={"id": "c2", "kind": "function_call", "input": "{}"}),
        RunStep("run_1", 3, StepKind.TOOL_CALL, "read_file", "stream",
                data={"id": "c3", "kind": "function_call", "input": '{"path": "b.txt"}'}),
    ]

    msgs = (await client.get(f"/api/threads/{key}/messages")).json()
    inputs = {m["blocks"][0]["id"]: m["blocks"][0]["input"] for m in msgs}
    assert inputs == {"c1": {"rfq_id": 7}, "c2": {}, "i1": {
        "server": "gateway", "tool": "lookup", "arguments": {"q": "x"}},
        "i2": "ls -la", "c3": {"path": "a.txt"}}


async def test_thread_transcript_keeps_showing_new_activity_after_200_messages(client, store):
    key = "chat:u1:long"
    await client.post("/api/chat", json={"text": "start", "conversation_id": "long"})
    store.messages.extend(
        Message(key, Role.ASSISTANT, [{"type": "text", "text": f"Update {i}"}])
        for i in range(205)
    )
    response = await client.get(f"/api/threads/{key}/messages")
    assert response.status_code == 200
    messages = response.json()
    assert len(messages) == 200
    assert messages[0]["blocks"][0]["text"] == "Update 5"
    assert messages[-1]["blocks"][0]["text"] == "Update 204"
