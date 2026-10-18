import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from handler.runtime.bus import RunEventBus
from handler.runtime.auth import Authenticator
from handler.runtime.context import RunContext
from handler.runtime.gateway.base import ToolResult
from handler.runtime.instructions import build_instructions
from handler.runtime.memory import MemoryTools
from handler.runtime.settings import SettingsConflict, SettingsService
from handler.runtime.store.sqlite import SQLiteCore
from handler.runtime.store.sqlite_memory import MemoryMixin, NotificationMixin, SettingsMixin
from handler.runtime.tools.notify import NotifyTools
from handler.runtime.types import MemoryTopic, Risk, ThreadType
from handler.runtime.web.settings_api import settings_router


class _Store(MemoryMixin, SettingsMixin, NotificationMixin, SQLiteCore):
    pass


@pytest.fixture
async def store(tmp_path):
    instance = _Store(tmp_path / "memory.db")
    yield instance
    await instance.close()


@pytest.fixture
def ctx():
    return RunContext("run", "chat:alice", "alice", "bob")


async def invoke(provider, tool_name, ctx, **args) -> ToolResult:
    return await next(spec.handler for spec in provider.tools() if spec.name == tool_name)(ctx, args)


async def test_memory_store_cas(store):
    first = await store.put_topic(MemoryTopic("a", "A", "one"), None)
    assert first.version == 1
    assert first.updated_at.tzinfo is not None
    assert await store.put_topic(MemoryTopic("a", "A", "lost"), None) is None
    results = await asyncio.gather(*[
        store.put_topic(MemoryTopic("a", "A", str(i)), first.version) for i in range(2)
    ])
    assert sum(result is not None for result in results) == 1
    assert (await store.get_topic("a")).version == 2
    assert await store.put_topic(MemoryTopic("missing", "", ""), 1) is None
    await store.put_topic(MemoryTopic("b", "B", "two"), None)
    assert [t.name for t in await store.list_topics()] == ["a", "b"]
    assert await store.delete_topic("a")
    assert not await store.delete_topic("a")


async def test_tools_and_concurrent_append(store, ctx):
    memory = MemoryTools(store)
    await invoke(memory, "memory_save", ctx, name="people/alice", content="first", description="Alice")
    results = await asyncio.gather(*[
        invoke(memory, "memory_save", ctx, name="people/alice", content=text, description="Updated")
        for text in ("second", "third")
    ])
    assert all(not result.is_error for result in results)
    topic = await store.get_topic("people/alice")
    assert set(topic.content.split("\n\n")) == {"first", "second", "third"}
    assert topic.version == 3 and topic.updated_by == "bob"
    assert (await invoke(memory, "memory_read", ctx, name=topic.name)).content == topic.content
    assert (await invoke(memory, "memory_list", ctx)).content == "people/alice: Updated"
    assert await memory.index_text(7) == "people/"
    assert await memory.index_text(0) == ""
    assert (await invoke(memory, "memory_rewrite", ctx, name=topic.name,
                         content="replacement", description="New")).is_error is False
    await invoke(memory, "memory_save", ctx, name="occupied", content="keep", description="")
    assert (await invoke(memory, "memory_rewrite", ctx, name=topic.name, content="bad",
                         description="", new_name="occupied")).is_error
    assert (await store.get_topic("occupied")).content == "keep"
    assert not (await invoke(memory, "memory_rewrite", ctx, name=topic.name,
                            content="moved", description="Moved", new_name="people/bob")).is_error
    assert await store.get_topic(topic.name) is None
    assert (await invoke(memory, "memory_delete", ctx, name="people/bob")).content == {"deleted": True}
    assert (await invoke(memory, "memory_read", ctx, name="missing")).is_error
    assert (await invoke(memory, "memory_rewrite", ctx, name="missing", content="", description="")).is_error
    for spec in memory.tools():
        assert spec.action.startswith("memory.")
        assert spec.risk == (Risk.READ if spec.name in {"memory_list", "memory_read"} else Risk.WRITE_INTERNAL)


@pytest.mark.parametrize("name", ["", "../a", "a..b", "Upper", "/root", "a.b", "a" * 101, "a b"])
async def test_invalid_names(store, ctx, name):
    memory = MemoryTools(store)
    for tool, args in [
        ("memory_read", {}), ("memory_delete", {}),
        ("memory_save", {"content": "", "description": ""}),
        ("memory_rewrite", {"content": "", "description": ""}),
    ]:
        with pytest.raises(ValueError):
            await invoke(memory, tool, ctx, name=name, **args)
    with pytest.raises(ValueError):
        await invoke(memory, "memory_rewrite", ctx, name="valid", content="", description="", new_name=name)


async def test_append_retry_exhaustion(ctx):
    class ConflictingStore:
        calls = 0

        async def get_topic(self, name):
            return None

        async def put_topic(self, topic, expected_version):
            self.calls += 1
            return None

    store = ConflictingStore()
    result = await invoke(MemoryTools(store), "memory_save", ctx, name="a", content="", description="")
    assert result.is_error and store.calls == 3


async def test_settings_defaults_validation_history(store):
    service = SettingsService(store)
    for section in service.schemas():
        value, version = await service.get(section)
        assert version is None
        assert value
        json.dumps(service.schemas()[section])
    value, _ = await service.get("threads")
    assert value["chat"]["harness"] == "codex"
    first = await service.put("persona", {"system": "first"}, "alice", None)
    assert first.version == 1 and first.updated_by == "alice"
    with pytest.raises(SettingsConflict):
        await service.put("persona", {"system": "lost"}, None, None)
    for section, value in [("limits", {"max_attempts": 0}), ("persona", {"extra": "x"}),
                           ("grants", {"defaults": {"read": "maybe"}}),
                           ("threads", {"chat": {"extra": "x"}})]:
        with pytest.raises(ValidationError):
            await service.put(section, value, None, None)
    second = await service.put("persona", {"system": "second"}, None, 1)
    restored = await service.rollback("persona", 1, "bob")
    assert restored.version == 3 and restored.value == first.value
    assert [v.version for v in await service.history("persona")] == [3, 2, 1]
    assert second.value["system"] == "second"
    with pytest.raises(ValueError):
        await service.rollback("persona", 99, None)
    with pytest.raises(KeyError):
        await service.get("unknown")


async def test_concurrent_settings_and_old_rollback(store):
    service = SettingsService(store)
    results = await asyncio.gather(*[
        store.put_settings("persona", {"system": str(i)}, updated_by=None, expected_version=None)
        for i in range(2)
    ])
    assert sum(result is not None for result in results) == 1
    original, _ = await service.get("persona")
    for version in range(1, 52):
        await service.put("persona", {"system": str(version)}, None, version)
    assert (await service.rollback("persona", 1, None)).value == original


async def test_seeding(store, tmp_path):
    config = tmp_path / "handler.yaml"
    agent = tmp_path / "AGENT.md"
    config.write_text("threads:\n  chat:\n    model: gpt-6-luna\nlimits:\n  max_attempts: 7\ngrants:\n  actions:\n    memory.*: allow\n")
    agent.write_text("ignored preamble\n# System\nSystem text\n## Subheading\nMore\n# Identity\nIdentity text\n# Persona\nPersona text\n")
    service = SettingsService(store)
    await service.seed_from_files(config, agent)
    persona, version = await service.get("persona")
    assert persona == {"system": "System text\n## Subheading\nMore", "identity": "Identity text", "persona": "Persona text"}
    assert version == 1
    assert (await service.get("threads"))[0]["chat"]["model"] == "gpt-6-luna"
    assert (await service.get("limits"))[0]["max_attempts"] == 7
    agent.write_text("# System\nOverwritten")
    await service.seed_from_files(config, agent)
    assert (await service.get("persona"))[0] == persona
    await service.seed_from_files(None, None)
    await service.seed_from_files(tmp_path / "missing", None)


@pytest.mark.parametrize("thread_type, guidance", [
    (ThreadType.CHAT, "Reply to the user"), (ThreadType.GMAIL, "draft rather than send"),
    (ThreadType.AUTOMATION, "use notify"),
])
async def test_instructions(store, thread_type, guidance):
    service = SettingsService(store)
    await service.put("persona", {"system": "System", "identity": "Identity", "persona": "Persona"}, None, None)
    await store.put_topic(MemoryTopic("people/alice", "Alice", "private detail"), None)
    instructions = await build_instructions(service, MemoryTools(store), speaker="alice", thread_type=thread_type)
    for fragment in ["System\n\nIdentity\n\nPersona", "# Memory", "people/alice: Alice",
                     "memory_read", "You are talking with alice", guidance,
                     "Durable facts go to memory_save, not files."]:
        assert fragment in instructions
    assert "private detail" not in instructions
    anonymous = await build_instructions(service, MemoryTools(store), speaker=None, thread_type=thread_type)
    assert "You are talking with" not in anonymous


async def test_notify_fanout(store, ctx):
    bus = RunEventBus()
    tools = NotifyTools(store, bus, users_provider=lambda: ["alice", "bob", "alice"])
    async with bus.subscribe() as queue:
        result = await invoke(tools, "notify", ctx, message="hello", to="all")
        assert len(result.content["notification_ids"]) == 2
        messages = [queue.get_nowait(), queue.get_nowait()]
        assert {msg.data["user_id"] for msg in messages} == {"alice", "bob"}
        assert all(msg.type == "notification" and msg.thread_key == ctx.thread_key for msg in messages)
        for message in messages:
            json.dumps(message.data)
    alice = (await store.list_notifications("alice", True))[0]
    assert alice.message == "hello" and alice.run_id == ctx.run_id
    assert await store.mark_read(alice.id)
    assert await store.mark_read(alice.id)
    assert not await store.list_notifications("alice", True)
    assert len(await store.list_notifications("alice", False)) == 1
    assert not await store.mark_read("missing")
    assert (await invoke(NotifyTools(store, bus), "notify", ctx, message="x", to="all")).is_error
    assert (await invoke(tools, "notify", ctx, message="x", to="alice")).is_error
    assert not (await invoke(tools, "notify", ctx, message="x", to=["carol"])).is_error


async def test_api(store, ctx):
    service = SettingsService(store)
    app = FastAPI()
    app.include_router(settings_router(service, Authenticator(store, "test-admin-key")))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": "Bearer test-admin-key"},
    ) as client:
        response = await client.get("/api/settings")
        assert set(response.json()) == {"persona", "threads", "grants", "limits"}
        assert response.json()["persona"]["version"] is None
        assert "schema" in response.json()["persona"]
        response = await client.put("/api/settings/persona", json={"system": "one"})
        assert response.status_code == 200 and response.json()["version"] == 1
        assert (await client.put("/api/settings/persona", json={"system": "lost"})).status_code == 409
        response = await client.put("/api/settings/persona", json={"system": "two"}, headers={"If-Match": "1"})
        assert response.status_code == 200 and response.json()["version"] == 2
        assert (await client.get("/api/settings/persona")).json()["value"]["system"] == "two"
        assert len((await client.get("/api/settings/persona/history")).json()) == 2
        response = await client.post("/api/settings/persona/rollback", json={"version": 1, "updated_by": "bob"})
        assert response.json()["version"] == 3 and response.json()["updated_by"] == "admin"
        assert (await client.post("/api/settings/persona/rollback", json={"version": 99})).status_code == 404
        assert (await client.put("/api/settings/limits", json={"max_attempts": 0})).status_code == 422
        assert (await client.put("/api/settings/persona", json={}, headers={"If-Match": "bad"})).status_code == 422
        assert (await client.get("/api/settings/unknown")).status_code == 404
        assert (await client.put("/api/settings/unknown", json={})).status_code == 404
        notification = await store.add_notification("alice", ctx.thread_key, ctx.run_id, "test")
        response = await client.get("/api/notifications", params={"user_id": "alice", "unread_only": True})
        assert response.json()[0]["id"] == notification.id
        assert (await client.post(f"/api/notifications/{notification.id}/read")).status_code == 200
        assert (await client.get("/api/notifications", params={"user_id": "alice", "unread_only": True})).json() == []
        assert (await client.post("/api/notifications/missing/read")).status_code == 404


async def test_persistence_and_clock(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    path = tmp_path / "persistent.db"
    store = _Store(path, clock=lambda: now)
    topic = await store.put_topic(MemoryTopic("a", "A", "body"), None)
    settings = await store.put_settings("persona", {"system": "hi"}, updated_by="alice", expected_version=None)
    notification = await store.add_notification("alice", "thread", None, "hi")
    assert topic.updated_at == settings.updated_at == notification.created_at == now
    await store.close()
    store = _Store(path)
    try:
        assert (await store.get_topic("a")).content == "body"
        assert (await store.get_settings("persona")).value == {"system": "hi"}
        assert (await store.list_notifications("alice"))[0].id == notification.id
    finally:
        await store.close()
