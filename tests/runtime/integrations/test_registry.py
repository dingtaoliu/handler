"""Integration registry: loading, dependencies, duplicates, per-instance config."""
import pytest
from fastapi import APIRouter

from handler.runtime import server
from handler.runtime.integrations import (
    Integration,
    IntegrationContext,
    IntegrationError,
    ThreadTypeSpec,
    enabled_names,
)

from .fakes import Alpha, Beta, FakeSink, Gamma, registry, tool


def ctx(name="main"):
    return IntegrationContext(instance_name=name, base_path="", env={}, store=None, bus=None,
                              settings=None, gateway=None, outbox=None, notifier=None, auth=None)


def test_enabled_names_unset_empty_and_scoped():
    assert enabled_names({}) is None
    assert enabled_names({"HANDLER_INTEGRATIONS": ""}) == []
    assert enabled_names({"HANDLER_INTEGRATIONS": " alpha , beta "}) == ["alpha", "beta"]
    # The scoped env has HANDLER_I_<NAME>__ stripped: INTEGRATIONS wins.
    assert enabled_names({"HANDLER_INTEGRATIONS": "alpha", "INTEGRATIONS": "gamma"}) == ["gamma"]
    with pytest.raises(IntegrationError, match="duplicate"):
        enabled_names({"HANDLER_INTEGRATIONS": "alpha,alpha"})


def test_per_instance_resolution_does_not_inherit_main(tmp_path):
    env = {
        "HANDLER_INSTANCES": "main,bss,ops",
        "HANDLER_RT_DATA_DIR": str(tmp_path / "main"),
        "HANDLER_ADMIN_KEY": "m", "HANDLER_I_BSS__HANDLER_ADMIN_KEY": "b",
        "HANDLER_I_OPS__HANDLER_ADMIN_KEY": "o",
        "HANDLER_INTEGRATIONS": "alpha",
        "HANDLER_I_BSS__INTEGRATIONS": "gamma",
    }
    configs = {name: scoped for name, _, scoped, _ in
               server.instance_configs(server._parse_args(["serve"], env), env)}
    assert enabled_names(configs["main"]) == ["alpha"]
    assert enabled_names(configs["bss"]) == ["gamma"]
    assert enabled_names(configs["ops"]) is None


def test_load_configures_and_collects_static_contributions():
    loaded = registry().load(["alpha", "beta"], instance_name="bss",
                             env={"ALPHA_TOKEN": "t"})
    assert loaded.names == ["alpha", "beta"]
    assert loaded.integrations[0].token == "t"
    assert loaded.integrations[0].config.enabled == ("alpha", "beta")
    assert set(loaded.thread_types) == {"alpha"}
    manifest = loaded.manifest()
    assert manifest["instance"] == "bss"
    assert [i["name"] for i in manifest["integrations"]] == ["alpha", "beta"]
    assert manifest["entries"] == [{"kind": "connection", "id": "alpha", "label": "Alpha",
                                    "path": "", "props": {}, "integration": "alpha"}]


def test_unknown_missing_dependency_and_missing_config_errors():
    with pytest.raises(IntegrationError, match=r"bss: unknown integration\(s\) nope"):
        registry().load(["nope"], instance_name="bss")
    with pytest.raises(IntegrationError, match="'beta' requires alpha"):
        registry().load(["beta"], instance_name="bss")
    with pytest.raises(IntegrationError, match="'alpha'.*ALPHA_TOKEN is required"):
        registry().load(["alpha"], instance_name="bss", env={})


def test_registry_rejects_duplicate_names():
    reg = registry(Alpha)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(Alpha)


def test_duplicate_thread_types_and_core_thread_types():
    class Other(Integration):
        name = "other"

        def thread_types(self):
            return [ThreadTypeSpec("alpha")]

    class Chatty(Integration):
        name = "chatty"

        def thread_types(self):
            return [ThreadTypeSpec("chat")]

    with pytest.raises(IntegrationError, match="thread type 'alpha' from 'other' duplicates"):
        registry(Alpha, Other).load(["alpha", "other"], env={"ALPHA_TOKEN": "t"})
    with pytest.raises(IntegrationError, match="core thread type"):
        registry(Chatty).load(["chatty"], reserved_thread_types=server.CORE_THREAD_TYPES)


def test_runtime_contributions_and_duplicates():
    loaded = registry().load(["alpha"], env={"ALPHA_TOKEN": "t"})
    out = loaded.collect_runtime(ctx())
    assert [owner for owner, _ in out.routers] == ["alpha"]
    assert [s.name for _, s in out.sinks] == ["alpha.sink"]
    assert [t.name for _, t in out.background_tasks] == ["poller"]
    assert out.identity_provider[0] == "alpha"

    with pytest.raises(IntegrationError, match="route GET /ingest/alpha from 'alpha' duplicates"):
        loaded.collect_runtime(ctx(), reserved_routes={("GET", "/ingest/alpha")})
    with pytest.raises(IntegrationError, match="tool 'alpha_ping' from 'alpha' duplicates"):
        loaded.collect_runtime(ctx(), reserved_tools={"alpha_ping"})


def test_two_identity_providers_and_duplicate_tools_sinks_routes_between_integrations():
    with pytest.raises(IntegrationError, match="only one identity provider"):
        registry().load(["alpha", "gamma"], env={"ALPHA_TOKEN": "t"}).collect_runtime(ctx())

    class Copy(Integration):
        name = "copy"
        kind = ""

        def routers(self, ctx):
            router = APIRouter()
            if self.kind == "route":
                router.add_api_route("/ingest/alpha", lambda: None)
            return [router]

        def tool_sets(self, ctx):
            return [tool("alpha_ping")] if self.kind == "tool" else []

        def sinks(self, ctx):
            return [FakeSink("alpha.sink")] if self.kind == "sink" else []

    for kind, message in [("route", "route GET /ingest/alpha from 'copy'"),
                          ("tool", "tool 'alpha_ping' from 'copy'"),
                          ("sink", "sink 'alpha.sink' from 'copy'")]:
        loaded = registry(Alpha, Copy).load(["alpha", "copy"], env={"ALPHA_TOKEN": "t"})
        loaded.integrations[1].kind = kind
        with pytest.raises(IntegrationError, match=message):
            loaded.collect_runtime(ctx())


def test_beta_requires_alpha_loads_in_order():
    loaded = registry(Alpha, Beta, Gamma).load(["beta", "alpha"], env={"ALPHA_TOKEN": "t"})
    assert loaded.names == ["beta", "alpha"]
    assert isinstance(loaded.integrations[0], Beta) and not isinstance(loaded, Gamma)
