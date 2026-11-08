from datetime import timedelta

import httpx
import pytest

from handler.runtime.auth import Authenticator
from handler.runtime.bus import RunEventBus
from handler.runtime.context import RunContext
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.store.sqlite_users import User
from handler.runtime.tools.threads import ThreadTools
from handler.runtime.types import Event, Lane, Message, Role, ThreadSpec, ThreadType, utcnow
from handler.runtime.web.app import create_app


@pytest.fixture
async def db(tmp_path):
    store = SQLiteStore(tmp_path / "metadata.db")
    for uid in ("one", "two"):
        await store.create_user(User(uid, uid))
    yield store
    await store.close()


async def seed(db, key, owner="one", kind=ThreadType.CHAT, text="Investigate supplier quotes"):
    await db.put(Event(type="chat.message", source="web", payload={"text": text},
                       idempotency_key=key),
                 ThreadSpec(key=key, type=kind, lane=Lane.INTERACTIVE,
                            owner_user_id=owner, harness="codex"))


async def message(db, key, role, stamp):
    from handler.runtime.store.sqlite import _insert, _values
    row = Message(key, role, [{"type": "text", "text": "hello"}], created_at=stamp)
    await db._call(lambda conn: _insert(conn, "messages", _values(row)))
    return row


async def test_watermarks_are_user_scoped_and_do_not_hide_concurrent_messages(db):
    key = "chat:one:first"
    await seed(db, key)
    now = utcnow()
    await message(db, key, Role.ASSISTANT, now)
    assert (await db.thread_metadata([key], "one"))[key]["unread"]
    await db.mark_thread_read(key, "one", now)
    assert not (await db.thread_metadata([key], "one"))[key]["unread"]
    assert (await db.thread_metadata([key], "two"))[key]["unread"]
    later = now + timedelta(seconds=1)
    await message(db, key, Role.ASSISTANT, later)
    await db.mark_thread_read(key, "one", now)  # delayed response from earlier transcript
    assert (await db.thread_metadata([key], "one"))[key]["unread"]
    await db.mark_thread_read(key, "one", now + timedelta(days=1))
    assert (await db.thread_metadata([key], "one"))[key]["read_at"] == later
    await db.mark_thread_read(key, "one", now)
    assert (await db.thread_metadata([key], "one"))[key]["read_at"] == later
    await db.mark_thread_read(key, "one", None)
    assert (await db.thread_metadata([key], "one"))[key]["read_at"] == later


async def test_titles_fallback_generated_and_manual_preserved(db):
    key = "chat:one:first"
    await seed(db, key)
    assert (await db.thread_metadata([key], "one"))[key]["title"] == "Investigate supplier quotes"
    tools = ThreadTools(db, RunEventBus())
    ctx = RunContext("run", key, "one")
    assert not (await tools._set_title(ctx, {"title": "Supplier quote comparison"})).is_error
    await db.set_thread_title(key, "  My custom  title ")
    await tools._set_title(ctx, {"title": "Agent replacement"})
    assert (await db.thread_metadata([key], "one"))[key]["title"] == "My custom title"
    for invalid in (" ", "x" * 121):
        with pytest.raises(ValueError):
            await db.set_thread_title(key, invalid)


async def test_list_activity_order_shared_threads_and_api_auth(db):
    await seed(db, "chat:one:old")
    await seed(db, "chat:two:private", "two")
    await seed(db, "automation:shared", "two", ThreadType.AUTOMATION)
    await seed(db, "chat:one:new")
    now = utcnow()
    await message(db, "chat:one:old", Role.ASSISTANT, now + timedelta(seconds=3))
    _, token = await db.create_token("one")
    app = create_app(db, RunEventBus(), Authenticator(db, "admin"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test",
                                headers={"Authorization": f"Bearer {token}"}) as client:
        rows = (await client.get("/api/threads?user_id=one")).json()
        assert rows[0]["key"] == "chat:one:old"
        assert {r["key"] for r in rows} == {"chat:one:old", "chat:one:new", "automation:shared"}
        assert rows[0]["unread"]
        assert (await client.put("/api/threads/chat:two:private/title", json={"title": "bad"})).status_code == 404
        assert (await client.put("/api/threads/automation:shared/title", json={"title": "bad"})).status_code == 403
        assert (await client.put("/api/threads/chat:one:old/title", json={"title": "Mine"})).json()["title"] == "Mine"
        assert (await client.post("/api/threads/chat:one:old/read", json={"last_message_at": (now + timedelta(seconds=3)).isoformat()})).status_code == 200
        assert not (await client.get("/api/threads?user_id=one")).json()[0]["unread"]
        assert (await client.post("/api/threads/chat:one:old/read", json={"last_message_at": "2026-10-18T00:00:00"})).status_code == 422
        assert (await client.post("/api/threads/chat:one:old/read", json={}, headers={"Authorization": "Bearer admin"})).status_code == 400


async def test_chat_context_links_accumulate_and_are_listed(db):
    _, token = await db.create_token("one")
    app = create_app(db, RunEventBus(), Authenticator(db, "admin-key-for-tests"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": f"Bearer {token}"}) as client:
        first = await client.post("/api/chat", json={
            "text": "about this RFQ", "conversation_id": "c1",
            "context_links": ["crontal:rfq:12", " crontal:rfq:12"]})
        assert first.status_code == 200
        assert first.json()["context_links"] == ["crontal:rfq:12"]
        key = first.json()["thread_key"]
        event = (await db.pending_events(key))[0]
        assert event.payload["context_links"] == ["crontal:rfq:12"]
        second = await client.post("/api/chat", json={
            "text": "and the bid", "conversation_id": "c1",
            "context_links": ["crontal:bid:4", "domain:Acme.com"]})
        assert second.json()["context_links"] == [
            "crontal:rfq:12", "crontal:bid:4", "domain:acme.com"]
        plain = await client.post("/api/chat", json={"text": "x", "conversation_id": "c1"})
        assert "context_links" not in plain.json()
        listed = next(t for t in (await client.get("/api/threads")).json() if t["key"] == key)
        assert listed["context_links"] == ["crontal:rfq:12", "crontal:bid:4", "domain:acme.com"]
        got = (await client.get(f"/api/threads/{key}")).json()["thread"]
        assert got["context_links"] == listed["context_links"]
        for bad in (["thread:chat:one:c1"], ["crontal:notification:1"], ["nope"],
                    [f"crontal:rfq:{n}" for n in range(6)]):
            r = await client.post("/api/chat", json={"text": "x", "context_links": bad})
            assert r.status_code == 422, bad
        fresh = await client.post("/api/chat", json={"text": "new"})
        fresh_key = fresh.json()["thread_key"]
        row = next(t for t in (await client.get("/api/threads")).json() if t["key"] == fresh_key)
        assert row["context_links"] is None
