"""POST /api/threads/import: legacy Crontal AI chats as Handler chat threads.

Both stores; Postgres runs with a plan input limit to show nothing is charged.
"""
from __future__ import annotations

import asyncio
import copy

import httpx
import pytest

from handler.runtime.auth import Authenticator
from handler.runtime.bus import RunEventBus
from handler.runtime.dispatcher import Dispatcher
from handler.runtime.harness.base import RunEvent, RunEventKind
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.run import RunConfig, RunExecutor
from handler.runtime.store.postgres import PostgresStore
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.store.sqlite_users import User
from handler.runtime.tenancy.limits import LiveLimits, PlanLimits
from handler.runtime.tenancy.plan_guard import allowed_without_console
from handler.runtime.types import Role, RunStatus
from handler.runtime.web.app import create_app
from handler.runtime.web.thread_import_api import MAX_MESSAGE_BYTES, MAX_MESSAGES

ADMIN_KEY = "test-admin-key-0123456789"
KEY = "chat:alice:legacy-42"


class RecordingHarness:
    name = "fake"

    def __init__(self):
        self.prompts: list[str] = []

    async def run(self, req):
        self.prompts.append(req.prompt)
        yield RunEvent(RunEventKind.SESSION, {"session_id": "sess-new"})
        yield RunEvent(RunEventKind.ASSISTANT_TEXT, {"text": "continued"})
        yield RunEvent(RunEventKind.FINAL, {"text": "continued"})

    async def interrupt(self, run_id):
        pass


def body(**changes) -> dict:
    payload = {
        "owner_user_id": "alice", "conversation_id": "legacy-42",
        "source": "crontal-legacy", "source_id": "42", "title": "Quote for 304 sheet",
        "created_at": "2026-03-01T09:00:00Z",
        "messages": [
            {"role": "user", "text": "Need 304 sheet 2mm, 5 t", "created_at": "2026-03-01T09:00:05Z"},
            {"role": "assistant", "text": "Drafted RFQ #77 for 5 t of 304 2B sheet.",
             "created_at": "2026-03-01T09:00:20Z"},
            {"role": "user", "text": "Add 316L too", "created_at": "2026-03-02T10:00:00Z"},
            {"role": "assistant", "text": "Added a 316L line to RFQ #77.",
             "created_at": "2026-03-02T10:00:30"},  # naive: taken as UTC
        ],
    }
    payload.update(changes)
    return payload


@pytest.fixture(params=["sqlite", "postgres"])
async def world(request, tmp_path):
    if request.param == "sqlite":
        store = SQLiteStore(tmp_path / "app.db")
    else:
        store = PostgresStore(request.getfixturevalue("pg_dsn"))
        store.limits = LiveLimits(PlanLimits("free", monthly_inputs=10))
    for name, role in (("root", "admin"), ("alice", "member"), ("bob", "member")):
        await store.create_user(User(name, name.title(), role))
    tokens = {name: (await store.create_token(name, "t"))[1] for name in ("root", "alice", "bob")}
    bus = RunEventBus()
    harness = RecordingHarness()
    executor = RunExecutor(store, {"fake": harness}, bus,
                           RunConfig(data_dir=tmp_path / "data"), PriceTable([]))
    dispatcher = Dispatcher(store, executor, lease_seconds=5)
    app = create_app(store, bus, Authenticator(store, ADMIN_KEY), default_harness="fake")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        yield {"store": store, "client": client, "tokens": tokens, "harness": harness,
               "dispatcher": dispatcher, "metered": request.param == "postgres"}
    await store.close()


ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}


def user(w, name):
    return {"Authorization": f"Bearer {w['tokens'][name]}"}


async def post(w, payload, headers=ADMIN):
    return await w["client"].post("/api/threads/import", headers=headers, json=payload)


async def used(w):
    data = (await w["client"].get("/api/limits/inputs", headers=user(w, "alice"))).json()
    return data["used"] if data["limited"] else None


async def test_import_creates_a_history_only_thread(world):
    w = world
    before = await used(w)
    response = await post(w, body())
    assert response.status_code == 201, response.text
    assert response.json() == {"thread_key": KEY, "conversation_id": "legacy-42",
                               "messages": 4, "created": True}
    store = w["store"]
    thread = await store.get_thread(KEY)
    assert thread.type == "chat" and thread.owner_user_id == "alice"
    assert thread.pending_since is None and thread.session_id is None
    assert thread.created_at.isoformat().startswith("2026-03-01T09:00:00")
    assert await store.pending_events(KEY) == []
    assert await store.list_runs(KEY) == []
    messages = await store.list_messages(KEY)
    assert [m.role for m in messages] == [Role.USER, Role.ASSISTANT, Role.USER, Role.ASSISTANT]
    assert messages[1].blocks == [{"type": "text",
                                   "text": "Drafted RFQ #77 for 5 t of 304 2B sheet."}]
    assert messages[3].created_at.isoformat() == "2026-03-02T10:00:30+00:00"
    assert await used(w) == before == (0 if w["metered"] else None)  # nothing charged


async def test_imported_thread_is_listed_by_its_last_message_and_read(world):
    w = world
    client = w["client"]
    newer = await client.post("/api/chat", headers=user(w, "alice"),
                              json={"text": "a new chat", "conversation_id": "fresh"})
    assert newer.status_code == 200
    assert (await post(w, body())).status_code == 201
    for headers in (user(w, "alice"), {**ADMIN, "X-Handler-User": "alice"}):
        rows = (await client.get("/api/threads?type=chat&user_id=alice",
                                 headers=headers)).json()
        assert [r["key"] for r in rows] == ["chat:alice:fresh", KEY]  # date order
        imported = rows[1]
        assert imported["title"] == "Quote for 304 sheet"
        assert imported["last_message_at"].startswith("2026-03-02T10:00:30")
        assert imported["unread"] is False and imported["pending_since"] is None
    detail = (await client.get(f"/api/threads/{KEY}", headers=user(w, "alice"))).json()
    assert detail["pending_events"] == []
    shown = (await client.get(f"/api/threads/{KEY}/messages", headers=user(w, "alice"))).json()
    assert [m["blocks"][0]["text"] for m in shown][-1] == "Added a 316L line to RFQ #77."
    bob = (await client.get("/api/threads", headers=user(w, "bob"))).json()
    assert KEY not in [r["key"] for r in bob]


async def test_repeat_is_idempotent_and_differences_conflict(world):
    w = world
    assert (await post(w, body())).status_code == 201
    again = await post(w, body(created_at="2026-03-01T09:00:00+00:00"))  # same instant
    assert again.status_code == 200
    assert again.json() == {"thread_key": KEY, "conversation_id": "legacy-42",
                            "messages": 4, "created": False}
    assert len(await w["store"].list_messages(KEY)) == 4

    changed = body()
    changed["messages"][0]["text"] = "something else"
    response = await post(w, changed)
    assert response.status_code == 409 and "different content" in response.json()["detail"]
    moved = await post(w, body(conversation_id="legacy-43"))  # same source chat, other key
    assert moved.status_code == 409 and KEY in moved.json()["detail"]
    taken = await post(w, body(source_id="99"))  # key exists from another source chat
    assert taken.status_code == 409 and "already exists" in taken.json()["detail"]
    await w["client"].post("/api/chat", headers=user(w, "alice"),
                           json={"text": "hi", "conversation_id": "live"})
    live = await post(w, body(conversation_id="live", source_id="100"))
    assert live.status_code == 409  # a live Handler chat is never overwritten
    assert len(await w["store"].list_messages(KEY)) == 4


async def test_concurrent_identical_imports_create_one_thread(world):
    w = world
    results = await asyncio.gather(*(post(w, body()) for _ in range(4)))
    assert sorted(r.status_code for r in results) == [200, 200, 200, 201]
    assert len(await w["store"].list_messages(KEY)) == 4


@pytest.mark.parametrize("change", [
    lambda b: b["messages"][0].update(role="system"),
    lambda b: b["messages"][0].update(role="tool"),
    lambda b: b.update(messages=[]),
    lambda b: b["messages"][0].update(text="   "),
    lambda b: b["messages"][0].update(text="x" * (MAX_MESSAGE_BYTES + 1)),
    lambda b: b["messages"][0].update(text="é" * (MAX_MESSAGE_BYTES // 2 + 1)),  # bytes, not chars
    lambda b: b.update(messages=[b["messages"][0]] * (MAX_MESSAGES + 1)),
    lambda b: b["messages"][0].update(created_at="yesterday"),
    lambda b: b.update(conversation_id="a/b"),
    lambda b: b.update(conversation_id=""),
    lambda b: b.update(source="Crontal Legacy"),
    lambda b: b.update(source_id=""),
    lambda b: b.update(extra="field"),
    lambda b: b.pop("created_at"),
])
async def test_bad_input_is_422(world, change):
    payload = copy.deepcopy(body())
    change(payload)
    response = await post(world, payload)
    assert response.status_code == 422, response.text
    assert await world["store"].get_thread(KEY) is None


async def test_total_size_cap_is_422(world):
    text = "y" * 90_000
    payload = body(messages=[{"role": "user", "text": text, "created_at": "2026-03-01T09:00:05Z"}]
                   * 60)  # 5.4 MB
    assert (await post(world, payload)).status_code == 422


async def test_unknown_owner_is_404(world):
    response = await post(world, body(owner_user_id="nobody"))
    assert response.status_code == 404
    assert await world["store"].get_thread("chat:nobody:legacy-42") is None


async def test_only_the_bare_admin_key_may_import(world):
    w = world
    for headers in (user(w, "alice"), user(w, "root"), {**ADMIN, "X-Handler-User": "root"}):
        response = await post(w, body(), headers=headers)
        assert response.status_code == 403, response.text
    assert (await post(w, body(), headers={})).status_code == 401
    assert await w["store"].get_thread(KEY) is None
    assert allowed_without_console("/api/threads/import")  # free plans may import


async def test_out_of_order_times_keep_the_given_order(world):
    payload = body()
    payload["messages"][2]["created_at"] = "2026-02-01T00:00:00Z"  # earlier than its predecessor
    assert (await post(world, payload)).status_code == 201
    messages = await world["store"].list_messages(KEY)
    assert [m.blocks[0]["text"] for m in messages] == [m["text"] for m in payload["messages"]]
    assert messages[2].created_at == messages[1].created_at


async def test_title_is_optional_and_falls_back_to_the_first_message(world):
    w = world
    assert (await post(w, body(title=None))).status_code == 201
    (row,) = (await w["client"].get("/api/threads?type=chat",
                                    headers=user(w, "alice"))).json()
    assert row["title"] == "Need 304 sheet 2mm, 5 t"


async def test_continuing_an_imported_chat_recaps_the_history(world):
    w = world
    assert (await post(w, body())).status_code == 201
    sent = await w["client"].post("/api/chat", headers=user(w, "alice"),
                                  json={"text": "Now add 310 round bar", "conversation_id": "legacy-42"})
    assert sent.status_code == 200 and sent.json()["thread_key"] == KEY
    dispatcher = w["dispatcher"]
    await dispatcher.claim_once()
    while dispatcher.active_count:
        await asyncio.sleep(0.01)
    (prompt,) = w["harness"].prompts
    assert prompt.startswith("Earlier conversation in this thread")
    assert "Drafted RFQ #77" in prompt and "Added a 316L line to RFQ #77." in prompt
    assert prompt.endswith("Current turn:\n" + prompt.split("Current turn:\n")[-1])
    assert "Now add 310 round bar" in prompt.split("Current turn:\n")[-1]
    assert "Now add 310 round bar" not in prompt.split("Current turn:\n")[0]
    (run,) = await w["store"].list_runs(KEY)
    assert run.status is RunStatus.COMPLETED
    thread = await w["store"].get_thread(KEY)
    assert thread.session_id == "sess-new" and thread.pending_since is None
    roles = [m.role for m in await w["store"].list_messages(KEY)]
    assert roles[:4] == [Role.USER, Role.ASSISTANT, Role.USER, Role.ASSISTANT]
    assert roles[4] == Role.USER and Role.ASSISTANT in roles[5:]
    assert await used(w) == (1 if w["metered"] else None)  # only the new message counts


async def test_recap_of_a_long_import_is_capped(world):
    w = world
    messages = [{"role": "user" if i % 2 == 0 else "assistant",
                 "text": f"m{i:04d} " + "z" * 5000,
                 "created_at": f"2026-03-01T09:{i // 60:02d}:{i % 60:02d}Z"} for i in range(300)]
    assert (await post(w, body(messages=messages))).status_code == 201
    await w["client"].post("/api/chat", headers=user(w, "alice"),
                           json={"text": "next", "conversation_id": "legacy-42"})
    await w["dispatcher"].claim_once()
    while w["dispatcher"].active_count:
        await asyncio.sleep(0.01)
    (prompt,) = w["harness"].prompts
    assert "m0299" in prompt and "m0280" in prompt and "m0279" not in prompt  # last 20
    assert len(prompt) < 20 * 600 + 500  # each message excerpted to 500 characters
