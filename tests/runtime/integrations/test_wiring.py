"""A fake integration is wired into only the instance that enables it; the manifest."""
import contextlib

import httpx
import pytest

from handler.runtime import server
from handler.runtime.integrations import IntegrationError

from ..test_multi_instance import config, headers
from .fakes import Alpha, registry


@contextlib.asynccontextmanager
async def serving(env, monkeypatch):
    async def idle(self, stop, *args):
        await stop.wait()
    monkeypatch.setattr(server.Dispatcher, "run_forever", idle)
    monkeypatch.setattr(server.Scheduler, "run_forever", idle)

    def no_codex():
        raise FileNotFoundError("test has no codex")
    monkeypatch.setattr(server, "codex_binary", no_codex)
    monkeypatch.setattr(server, "default_registry", registry())
    host = server.create_host(server._parse_args(["serve"], env), env)
    async with host.router.lifespan_context(host):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host),
                                     base_url="http://handler") as client:
            yield client, host


def instance_app(host, name):
    return next(r.app for r in host.routes if getattr(r, "path", "") == f"/i/{name}")


async def test_fake_integration_is_wired_only_where_enabled(tmp_path, monkeypatch):
    Alpha.started.clear()
    env = {**config(tmp_path), "HANDLER_I_BSS__INTEGRATIONS": "alpha",
           "HANDLER_I_BSS__ALPHA_TOKEN": "bss-token"}
    async with serving(env, monkeypatch) as (client, host):
        bss, main = instance_app(host, "bss"), instance_app(host, "main")
        # Router under the instance prefix only.
        response = await client.get("/i/bss/ingest/alpha")
        assert response.json() == {"instance": "bss", "token": "bss-token"}
        assert (await client.get("/i/main/ingest/alpha")).status_code == 404
        assert (await client.get("/ingest/alpha")).status_code == 404
        # Tool into the gateway, sink into the outbox, thread type and provider collected.
        assert "alpha_ping" in {s.name for s in bss.state.gateway.specs()}
        assert "alpha_ping" not in {s.name for s in main.state.gateway.specs()}
        assert "alpha.sink" in bss.state.outbox.sinks
        assert "alpha.sink" not in main.state.outbox.sinks
        assert set(bss.state.integrations.thread_types) == {"alpha"}
        assert main.state.integrations.thread_types == {}
        assert bss.state.identity_provider is not None
        assert main.state.identity_provider is None
        # Background task started under the instance lifespan.
        assert Alpha.started == ["bss"]

        # Manifest: authenticated, per instance.
        assert (await client.get("/i/bss/api/ui/manifest")).status_code == 401
        assert (await client.get("/i/bss/api/ui/manifest",
                                 headers=headers("main-admin"))).status_code == 401
        body = (await client.get("/i/bss/api/ui/manifest", headers=headers("bss-admin"))).json()
        assert body["instance"] == "bss" and body["configured"] is True
        assert [i["name"] for i in body["integrations"]] == ["alpha"]
        assert {"kind": "connection", "id": "alpha", "label": "Alpha", "path": "",
                "props": {}, "integration": "alpha"} in body["entries"]
        main_body = (await client.get("/api/ui/manifest", headers=headers("main-admin"))).json()
        assert main_body["instance"] == "main" and main_body["configured"] is False
        assert main_body["integrations"] == []
        assert main_body["entries"] and {e["integration"] for e in main_body["entries"]} == {"core"}


async def test_unknown_integration_fails_startup(tmp_path, monkeypatch):
    env = {**config(tmp_path), "HANDLER_I_BSS__INTEGRATIONS": "nope"}
    with pytest.raises(IntegrationError, match="bss: unknown integration"):
        async with serving(env, monkeypatch):
            pass


async def test_missing_integration_config_fails_startup(tmp_path, monkeypatch):
    env = {**config(tmp_path), "HANDLER_I_BSS__INTEGRATIONS": "alpha"}
    with pytest.raises(IntegrationError, match="ALPHA_TOKEN is required"):
        async with serving(env, monkeypatch):
            pass
