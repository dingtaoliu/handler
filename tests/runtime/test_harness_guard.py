"""HANDLER_ALLOWED_HARNESSES: the host allows only listed harnesses, everywhere one is chosen."""

from __future__ import annotations

import os

import httpx
import pytest

from handler.runtime import server
from handler.runtime.auth import Authenticator
from handler.runtime.bus import RunEventBus
from handler.runtime.harness.base import RunEvent, RunEventKind
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.run import RunConfig, RunExecutor
from handler.runtime.settings import (
    HarnessNotAllowed,
    SettingsService,
    parse_allowed_harnesses,
)
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import Event, Lane, RunStatus, ThreadSpec, ThreadType
from handler.runtime.web.app import create_app
from handler.runtime.web.settings_api import settings_router

ADMIN = "admin-key-for-tests"
NATIVE = frozenset({"openai_agents"})


@pytest.fixture
async def store(tmp_path):
    s = SQLiteStore(tmp_path / "guard.db")
    yield s
    await s.close()


def test_parse_allowed_harnesses():
    assert parse_allowed_harnesses(None) is None
    assert parse_allowed_harnesses(" ") is None
    assert parse_allowed_harnesses("openai_agents") == NATIVE
    assert parse_allowed_harnesses("codex, openai_agents") == {"codex", "openai_agents"}
    with pytest.raises(ValueError, match="unknown harness"):
        parse_allowed_harnesses("openai_agents,claude")


async def test_saving_threads_settings_with_a_disallowed_harness_is_refused(store):
    service = SettingsService(store)
    service.allowed_harnesses = NATIVE
    with pytest.raises(HarnessNotAllowed, match="threads.chat"):
        await service.put("threads", {"chat": {"harness": "codex"},
                                      "automation": {"harness": "openai_agents"}}, "t", None)
    saved = await service.put("threads", {"chat": {"harness": "openai_agents"}}, "t", None)
    # Types left out start on a harness the host allows, never Codex.
    assert {entry["harness"] for entry in saved.value.values()} == {"openai_agents"}


async def test_nothing_saved_defaults_to_an_allowed_harness(store):
    service = SettingsService(store)
    assert (await service.get("threads"))[0]["chat"]["harness"] == "codex"
    service.allowed_harnesses = NATIVE
    threads, _ = await service.get("threads")
    assert {entry["harness"] for entry in threads.values()} == {"openai_agents"}
    assert await service.harness_source("mail")() == "openai_agents"
    schema = service.schemas()["threads"]["$defs"]
    assert schema["HarnessName"]["enum"] == ["openai_agents"]


async def test_settings_api_rejects_and_rollback_cannot_restore_a_disallowed_harness(store):
    service = SettingsService(store)
    old = await service.put("threads", {"chat": {"harness": "codex"}}, "t", None)  # before the guard
    await service.put("threads", {"chat": {"harness": "openai_agents"}}, "t", old.version)
    service.allowed_harnesses = NATIVE
    auth = Authenticator(store, ADMIN)
    app = create_app(store, RunEventBus(), auth)
    app.include_router(settings_router(service, auth))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": f"Bearer {ADMIN}"}) as client:
        response = await client.put("/api/settings/threads", json={"chat": {"harness": "codex"}})
        assert response.status_code == 422
        assert "HANDLER_ALLOWED_HARNESSES" in response.json()["detail"]
        response = await client.post("/api/settings/threads/rollback",
                                     json={"version": old.version})
        assert response.status_code == 422
        current = (await client.get("/api/settings/threads")).json()["version"]
        response = await client.put("/api/settings/threads", headers={"If-Match": str(current)},
                                    json={"chat": {"harness": "openai_agents"}})
        assert response.status_code == 200


class RecordingHarness:
    def __init__(self, name):
        self.name = name
        self.runs = 0

    async def run(self, req):
        self.runs += 1
        yield RunEvent(RunEventKind.FINAL, {"text": "ran"})

    async def interrupt(self, run_id):
        pass


async def test_a_thread_on_a_disallowed_harness_is_refused_not_switched(store, tmp_path):
    codex, native = RecordingHarness("codex"), RecordingHarness("openai_agents")
    key = "automation:old"
    await store.put(Event("automation.fired", "test", {"prompt": "go"}, "e1", thread_key=key),
                    ThreadSpec(key, ThreadType.AUTOMATION, Lane.BACKGROUND, harness="codex"))
    lease, = await store.claim("w", Lane.BACKGROUND, 1, 60)
    executor = RunExecutor(store, {"codex": codex, "openai_agents": native}, RunEventBus(),
                           RunConfig(data_dir=tmp_path / "data", allowed_harnesses=NATIVE),
                           PriceTable([]))
    await executor.execute(lease)
    assert codex.runs == native.runs == 0
    run, = await store.list_runs(key)
    assert run.status is RunStatus.FAILED and "not allowed on this host" in run.error
    thread = await store.get_thread(key)
    assert thread.dead and thread.harness == "codex" and thread.attempts == 1  # no retry


async def _wire(tmp_path, monkeypatch, store, argv, allowed):
    monkeypatch.setattr(store, "close", _noop_close)  # instance_runtime closes it; reused
    """instance_runtime up to the executor; returns what it built (or raises)."""
    monkeypatch.setattr(server, "SQLiteStore", lambda path: store)
    monkeypatch.setattr(server, "codex_binary", lambda: "unused")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HANDLER_DEFAULT_HARNESS", raising=False)
    captured = {}

    class WiringComplete(Exception):
        pass

    def capture_executor(store_, harnesses, bus, config, *args, **kwargs):
        captured.update(harnesses=harnesses, config=config, settings=kwargs["settings"])
        raise WiringComplete

    monkeypatch.setattr(server, "RunExecutor", capture_executor)
    args = server._parse_args(["serve", "--data-dir", str(tmp_path / "data"), *argv])
    env = {**os.environ, "OPENAI_API_KEY": "fake"}
    env.pop("HANDLER_DEFAULT_HARNESS", None)
    with pytest.raises(WiringComplete):
        async with server.instance_runtime(args, env, allowed_harnesses=allowed):
            pass
    return captured


async def test_host_with_native_only_never_starts_codex(store, tmp_path, monkeypatch):
    captured = await _wire(tmp_path, monkeypatch, store, [], NATIVE)
    assert set(captured["harnesses"]) == {"openai_agents"}
    assert captured["config"].allowed_harnesses == NATIVE
    assert captured["settings"].allowed_harnesses == NATIVE
    unrestricted = await _wire(tmp_path, monkeypatch, store, [], None)
    assert set(unrestricted["harnesses"]) == {"codex", "openai_agents"}


async def test_default_harness_outside_the_allow_list_stops_startup(store, tmp_path, monkeypatch):
    with pytest.raises(HarnessNotAllowed, match="HANDLER_DEFAULT_HARNESS"):
        await _wire(tmp_path, monkeypatch, store, ["--default-harness", "codex"], NATIVE)


def test_allow_list_is_host_only():
    assert "HANDLER_ALLOWED_HARNESSES" in server._HOST_ONLY_ENV
    inherited = server._inheritable_env({"HANDLER_ALLOWED_HARNESSES": "openai_agents"})
    assert "HANDLER_ALLOWED_HARNESSES" not in inherited


async def _noop_close():
    pass
