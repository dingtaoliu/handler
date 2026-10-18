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
        body = (await c.get("/api/settings/threads")).json()
        d = body["defaults"]
        # Instance default fills nulls; harness is always concrete.
        assert d["chat"] == {"harness": "codex", "model": "gpt-6-sol", "reasoning_effort": "medium"}
        assert set(d) == {"chat", "gmail", "automation", "crontal"}
        assert set(body["harness_defaults"]) == {"codex", "openai_agents"}
        await service.put("threads", {"chat": {"harness": "openai_agents", "model": "gpt-6-luna",
                                               "reasoning_effort": "high"}}, "t", None)
        d = (await c.get("/api/settings/threads")).json()["defaults"]
        # Defaults label the empty option, so an override set on the field
        # doesn't leak into them: they say what clearing it would give.
        assert d["chat"] == {"harness": "openai_agents", "model": "gpt-6-sol",
                             "reasoning_effort": "medium"}
        assert d["gmail"]["model"] == "gpt-6-sol"
        assert (await c.get("/api/settings/persona")).json()["defaults"] == {}
        assert "harness_defaults" not in (await c.get("/api/settings/persona")).json()
        assert "defaults" in (await c.get("/api/settings")).json()["threads"]


async def test_harness_builtin_default(store, user_token):
    # No harness to ask (e.g. codex not installed): static fallbacks only.
    service = SettingsService(store, InstanceDefaults(None, "low"))
    await service.put("threads", {"chat": {"harness": "openai_agents"}}, "t", None)
    async with make_client(store, service, user_token) as c:
        d = (await c.get("/api/settings/threads")).json()["defaults"]
    assert d["chat"]["model"] == "gpt-6.1-sol"
    assert d["gmail"] == {"harness": "codex", "model": None, "reasoning_effort": "low"}


def fake_harness_defaults(calls=None):
    table = {"codex": {"model": "gpt-6-astra", "reasoning_effort": "medium"},
             "openai_agents": {"model": "gpt-6.1-sol", "reasoning_effort": None}}

    async def lookup(name):
        if calls is not None:
            calls.append(name)
        return table.get(name, {})
    return lookup


async def test_defaults_ask_the_harness_when_instance_unset(store, user_token):
    service = SettingsService(store, InstanceDefaults(None, None), fake_harness_defaults())
    await service.put("threads", {"chat": {"harness": "openai_agents"},
                                  "gmail": {"harness": "codex", "model": "gpt-6-luna"}}, "t", None)
    async with make_client(store, service, user_token) as c:
        body = (await c.get("/api/settings/threads")).json()
    d, by_harness = body["defaults"], body["harness_defaults"]
    assert d["chat"] == {"harness": "openai_agents", "model": "gpt-6.1-sol",
                         "reasoning_effort": None}
    # Codex's own default (its catalog's first model and that model's level).
    assert d["gmail"] == {"harness": "codex", "model": "gpt-6-astra", "reasoning_effort": "medium"}
    # Per-harness table for relabelling when the harness select changes.
    assert by_harness == {
        "codex": {"model": "gpt-6-astra", "reasoning_effort": "medium"},
        "openai_agents": {"model": "gpt-6.1-sol", "reasoning_effort": None},
    }


async def test_instance_default_beats_harness_default(store, user_token):
    service = SettingsService(store, InstanceDefaults("gpt-5.6-sol", "xhigh"),
                              fake_harness_defaults())
    async with make_client(store, service, user_token) as c:
        body = (await c.get("/api/settings/threads")).json()
    assert body["defaults"]["chat"] == {"harness": "codex", "model": "gpt-5.6-sol",
                                        "reasoning_effort": "xhigh"}
    assert all(v == {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"}
               for v in body["harness_defaults"].values())


async def test_prompt_endpoint(store, user_token):
    service = SettingsService(store)
    await service.put("persona", {"system": "SYS-TEXT", "persona": "PERSONA-TEXT"}, "t", None)
    async with make_client(store, service, user_token) as c:
        r = await c.get("/api/settings/prompt", params={"thread_type": "chat"})
        assert r.status_code == 200 and r.json()["thread_type"] == "chat"
        text = r.json()["prompt"]
        assert "SYS-TEXT" in text and "PERSONA-TEXT" in text
        assert "You are talking with u1" in text and "Reply to the user." in text
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


async def test_chat_options_follow_settings_and_thread(store, user_token):
    service = SettingsService(store, InstanceDefaults(None, "low"), fake_harness_defaults())
    await service.put("threads", {"chat": {"harness": "codex", "reasoning_effort": "max"}},
                      "t", None)
    async with make_client(store, service, user_token) as c:
        new = (await c.get("/api/chat/options")).json()
        assert (new["harness"], new["default_model"], new["default_reasoning_effort"]) == (
            "codex", "gpt-6-astra", "max")
        # An existing thread keeps its own harness and model.
        await store.put(
            Event(type="chat.message", source="t", payload={"text": "x"}, idempotency_key="o1",
                  thread_key="chat:u1:old", user_id="u1"),
            ThreadSpec("chat:u1:old", ThreadType.CHAT, Lane.INTERACTIVE, owner_user_id="u1",
                       harness="openai_agents", model="gpt-6-luna"))
        old = (await c.get("/api/chat/options", params={"thread_key": "chat:u1:old"})).json()
        assert (old["harness"], old["default_model"], old["default_reasoning_effort"]) == (
            "openai_agents", "gpt-6-luna", "max")
        # Unknown threads fall back to a new chat's defaults.
        missing = (await c.get("/api/chat/options", params={"thread_key": "chat:u1:nope"})).json()
        assert missing["default_model"] == "gpt-6-astra"


async def test_chat_options_ignore_other_users_thread(store, user_token):
    await store.create_user(User("u2", "Two"))
    service = SettingsService(store, InstanceDefaults("gpt-6-sol", "low"))
    await store.put(
        Event(type="chat.message", source="t", payload={"text": "x"}, idempotency_key="o2",
              thread_key="chat:u2:c", user_id="u2"),
        ThreadSpec("chat:u2:c", ThreadType.CHAT, Lane.INTERACTIVE, owner_user_id="u2",
                   harness="openai_agents", model="gpt-6-luna"))
    async with make_client(store, service, user_token) as c:
        r = (await c.get("/api/chat/options", params={"thread_key": "chat:u2:c"})).json()
    assert r["default_model"] == "gpt-6-sol" and r["harness"] == "codex"


async def test_api_default_matches_what_the_run_uses(store, tmp_path, user_token):
    """The label's value is what a run with no choices actually gets."""
    service = SettingsService(store, InstanceDefaults(None, "low"), fake_harness_defaults())
    await service.put("threads", {"chat": {"harness": "codex", "reasoning_effort": "high"}},
                      "t", None)
    async with make_client(store, service, user_token) as c:
        opts = (await c.get("/api/chat/options")).json()
    (req,) = await run_chat(store, tmp_path, [{"text": "a"}], settings=service)
    # _plan passes the settings value; the harness fills the unset model itself.
    assert req.reasoning_effort == opts["default_reasoning_effort"] == "high"
    assert req.model is None
    assert (await fake_harness_defaults()("codex"))["model"] == opts["default_model"]


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
