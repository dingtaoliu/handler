"""Settings defaults, rendered prompt, chat options and per-message model overrides."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from handler.runtime.auth import Authenticator
from handler.runtime.bus import RunEventBus
from handler.runtime.dispatcher import Dispatcher
from handler.runtime.harness.base import RunEvent, RunEventKind
from handler.runtime.memory import MemoryTools
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.run import RunConfig, RunExecutor
from handler.runtime.settings import InstanceDefaults, ModelName, ReasoningEffort, SettingsService
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.store.sqlite_users import User
from handler.runtime.types import Event, Lane, ThreadSpec, ThreadType
from handler.runtime.web.app import create_app
from handler.runtime.web.settings_api import settings_router

ADMIN = "admin-key-for-tests"


@pytest.fixture
async def store(tmp_path):
    s = SQLiteStore(tmp_path / "m.db")
    await s.create_user(User("u1", "Una One"))
    yield s
    await s.close()


def make_client(store, service, token):
    auth = Authenticator(store, ADMIN)
    app = create_app(store, RunEventBus(), auth)
    app.include_router(settings_router(service, auth))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
async def user_token(store):
    return (await store.create_token("u1"))[1]


async def test_defaults_resolution(store, user_token):
    service = SettingsService(store, InstanceDefaults("gpt-6-sol", "medium"))
    async with make_client(store, service, user_token) as c:
        d = (await c.get("/api/settings/threads")).json()["defaults"]
        # Instance default fills nulls; harness is always concrete.
        assert d["chat"] == {"harness": "codex", "model": "gpt-6-sol", "reasoning_effort": "medium"}
        assert set(d) == {"chat", "gmail", "automation", "crontal"}
        await service.put("threads", {"chat": {"harness": "openai_agents", "model": "gpt-6-luna",
                                               "reasoning_effort": "high"}}, "t", None)
        d = (await c.get("/api/settings/threads")).json()["defaults"]
        assert d["chat"] == {"harness": "openai_agents", "model": "gpt-6-luna",
                             "reasoning_effort": "high"}
        assert d["gmail"]["model"] == "gpt-6-sol"
        assert (await c.get("/api/settings/persona")).json()["defaults"] == {}
        assert "defaults" in (await c.get("/api/settings")).json()["threads"]


async def test_harness_builtin_default(store, user_token):
    service = SettingsService(store, InstanceDefaults(None, "low"))
    await service.put("threads", {"chat": {"harness": "openai_agents"}}, "t", None)
    async with make_client(store, service, user_token) as c:
        d = (await c.get("/api/settings/threads")).json()["defaults"]
    assert d["chat"]["model"] == "gpt-6.1-sol"
    assert d["gmail"] == {"harness": "codex", "model": None, "reasoning_effort": "low"}


async def test_prompt_endpoint(store, user_token):
    service = SettingsService(store)
    await service.put("persona", {"system": "SYS-TEXT", "persona": "PERSONA-TEXT"}, "t", None)
    async with make_client(store, service, user_token) as c:
        r = await c.get("/api/settings/prompt", params={"thread_type": "chat"})
        assert r.status_code == 200 and r.json()["thread_type"] == "chat"
        text = r.json()["prompt"]
        assert "SYS-TEXT" in text and "PERSONA-TEXT" in text
        assert "You are talking with Una One" in text and "Reply to the user." in text
        r = await c.get("/api/settings/prompt", params={"thread_type": "automation"})
        assert "scheduled job" in r.json()["prompt"]
        assert "You are talking with" not in r.json()["prompt"]
        assert (await c.get("/api/settings/prompt", params={"thread_type": "nope"})).status_code == 422
    async with make_client(store, service, "bad-token") as anon:
        r = await anon.get("/api/settings/prompt", params={"thread_type": "chat"})
        assert r.status_code in (401, 403)


async def test_chat_options(store, user_token):
    service = SettingsService(store, InstanceDefaults("gpt-6-sol", "high"))
    async with make_client(store, service, user_token) as c:
        r = await c.get("/api/chat/options")
    assert r.json() == {
        "models": [m.value for m in ModelName],
        "reasoning_efforts": [e.value for e in ReasoningEffort],
        "default_model": "gpt-6-sol", "default_reasoning_effort": "high", "harness": "codex",
    }


async def test_chat_override_validation_and_payload(store, user_token):
    service = SettingsService(store)
    async with make_client(store, service, user_token) as c:
        bad = await c.post("/api/chat", json={"text": "a", "model": "gpt-9"})
        assert bad.status_code == 422
        bad = await c.post("/api/chat", json={"text": "a", "reasoning_effort": "ultra"})
        assert bad.status_code == 422
        ok = await c.post("/api/chat", json={"text": "a", "conversation_id": "c1",
                                             "model": "gpt-6-luna", "reasoning_effort": "max"})
        assert ok.status_code == 200
        plain = await c.post("/api/chat", json={"text": "b", "conversation_id": "c2",
                                                "model": None})
        assert plain.status_code == 200
    (ev,) = await store.pending_events("chat:u1:c1")
    assert ev.payload["model"] == "gpt-6-luna" and ev.payload["reasoning_effort"] == "max"
    (ev2,) = await store.pending_events("chat:u1:c2")
    assert "model" not in ev2.payload and "reasoning_effort" not in ev2.payload


class Recorder:
    name = "fake"

    def __init__(self):
        self.requests = []

    async def run(self, req):
        self.requests.append(req)
        yield RunEvent(RunEventKind.FINAL, {"text": "ok", "session_id": "s"})

    async def interrupt(self, run_id):
        pass


async def run_chat(store, tmp_path, payloads, settings=None):
    harness = Recorder()
    config = RunConfig(data_dir=tmp_path / "data", heartbeat_seconds=0.05, lease_seconds=5,
                       reasoning_effort="low")
    executor = RunExecutor(store, {"fake": harness}, RunEventBus(), config,
                           PriceTable([]),
                           settings=settings,
                           memory_tools=MemoryTools(store) if settings else None)
    dispatcher = Dispatcher(store, executor, lease_seconds=5)
    for i, payload in enumerate(payloads):
        ev = Event(type="chat.message", source="t", payload=payload, idempotency_key=f"k{i}",
                   thread_key="chat:u1:c", user_id="u1")
        await store.put(ev, ThreadSpec("chat:u1:c", ThreadType.CHAT, Lane.INTERACTIVE,
                                       owner_user_id="u1", harness="fake"))
    await dispatcher.claim_once()
    while dispatcher.active_count:
        await asyncio.sleep(0.01)
    return harness.requests


async def test_override_reaches_run_request(store, tmp_path):
    service = SettingsService(store)
    await service.put("threads", {"chat": {"model": "gpt-6-sol", "reasoning_effort": "medium"}},
                      "t", None)
    (req,) = await run_chat(store, tmp_path, [
        {"text": "a", "model": "gpt-6-astra"},
        {"text": "b", "model": "gpt-6-luna", "reasoning_effort": "max"},
    ], settings=service)
    assert req.model == "gpt-6-luna" and req.reasoning_effort == "max"
    (run,) = await store.list_runs("chat:u1:c")
    assert run.model == "gpt-6-luna"


async def test_no_override_keeps_existing_resolution(store, tmp_path):
    service = SettingsService(store)
    await service.put("threads", {"chat": {"model": "gpt-6-sol"}}, "t", None)
    (req,) = await run_chat(store, tmp_path, [{"text": "a"}], settings=service)
    assert req.model == "gpt-6-sol" and req.reasoning_effort == "low"


async def test_no_override_no_settings(store, tmp_path):
    (req,) = await run_chat(store, tmp_path, [{"text": "a"}])
    assert req.model is None and req.reasoning_effort == "low"
