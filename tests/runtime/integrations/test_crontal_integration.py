"""The `crontal` integration wires exactly what the legacy Crontal blocks did."""
import contextlib

import httpx

from handler.runtime import server
from handler.runtime.integrations import default_registry
from handler.runtime.integrations.crontal import connector as crontal_connector_module
from handler.runtime.integrations.registry import route_keys
from handler.runtime.types import ThreadType

from ..test_multi_instance import config

CRONTAL_ENV = {
    "CRONTAL_URL": "http://crontal.invalid/api/v1/agent-mcp",
    "CRONTAL_AGENT_TOKEN": "main-agent",
    "HANDLER_I_BSS__CRONTAL_URL": "http://crontal.invalid/api/v1/agent-mcp",
    "HANDLER_I_BSS__CRONTAL_AGENT_TOKEN": "bss-agent",
}


@contextlib.asynccontextmanager
async def serving(env, monkeypatch):
    async def idle(self, stop, *args):
        await stop.wait()
    monkeypatch.setattr(server.Dispatcher, "run_forever", idle)
    monkeypatch.setattr(server.Scheduler, "run_forever", idle)

    def no_codex():
        raise FileNotFoundError("test has no codex")
    monkeypatch.setattr(server, "codex_binary", no_codex)
    calls = []

    async def fake_connector(url, token, gateway, stop):
        if url and token:
            calls.append((url, token, id(gateway)))
    monkeypatch.setattr(server, "run_crontal_connector", fake_connector)
    monkeypatch.setattr(crontal_connector_module, "run_crontal_connector", fake_connector)
    host = server.create_host(server._parse_args(["serve"], env), env)
    async with host.router.lifespan_context(host):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host),
                                     base_url="http://handler") as client:
            yield client, host, calls


def instance_app(host, name):
    return next(r.app for r in host.routes if getattr(r, "path", "") == f"/i/{name}")


def snapshot(app, calls):
    gateway = app.state.gateway
    return {
        "routes": set(route_keys(app)),
        "tools": {s.name for s in gateway.specs()},
        "sinks": set(app.state.outbox.sinks),
        "thread_types": {t.value for t in ThreadType} | set(app.state.integrations.thread_types),
        "connector": sorted((url, token) for url, token, gid in calls if gid == id(gateway)),
    }


def crontal_parts(snap):
    return {
        "routes": {key for key in snap["routes"] if "crontal" in key[1]},
        "tools": {name for name in snap["tools"] if name.startswith("crontal")},
        "sinks": {name for name in snap["sinks"] if name.startswith("crontal")},
        "connector": snap["connector"],
    }


async def snapshots(tmp_path, monkeypatch, extra):
    env = {**config(tmp_path), **CRONTAL_ENV, **extra}
    async with serving(env, monkeypatch) as (_, host, calls):
        return {name: snapshot(instance_app(host, name), calls) for name in ("main", "bss")}


def test_crontal_is_registered():
    assert "crontal" in default_registry.names()


async def test_enabled_matches_legacy(tmp_path, monkeypatch):
    legacy = await snapshots(tmp_path / "legacy", monkeypatch, {})
    enabled = await snapshots(tmp_path / "enabled", monkeypatch, {
        "HANDLER_INTEGRATIONS": "crontal", "HANDLER_I_BSS__INTEGRATIONS": "crontal"})
    for name in ("main", "bss"):
        assert enabled[name] == legacy[name], name
        parts = crontal_parts(legacy[name])
        # Legacy really wires Crontal here, so the comparison is meaningful.
        assert ("POST", "/ingest/crontal") in parts["routes"]
        assert parts["tools"] and parts["sinks"] == {"crontal.notifications"}
        assert len(parts["connector"]) == 1
    assert "crontal" in enabled["bss"]["thread_types"]


async def test_empty_integrations_has_no_crontal(tmp_path, monkeypatch):
    snaps = await snapshots(tmp_path, monkeypatch, {
        "HANDLER_INTEGRATIONS": "", "HANDLER_I_BSS__INTEGRATIONS": ""})
    for name in ("main", "bss"):
        assert crontal_parts(snaps[name]) == {"routes": set(), "tools": set(),
                                              "sinks": set(), "connector": []}, name


async def test_enabled_on_one_instance_only(tmp_path, monkeypatch):
    legacy = await snapshots(tmp_path / "legacy", monkeypatch, {})
    snaps = await snapshots(tmp_path / "mixed", monkeypatch, {
        "HANDLER_INTEGRATIONS": "", "HANDLER_I_BSS__INTEGRATIONS": "crontal"})
    assert crontal_parts(snaps["main"]) == {"routes": set(), "tools": set(),
                                            "sinks": set(), "connector": []}
    assert snaps["bss"] == legacy["bss"]
    assert set(snaps["bss"]["thread_types"]) >= {"crontal"}
