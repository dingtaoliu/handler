"""In-tree integration registry and per-instance loading.

`HANDLER_INTEGRATIONS=crontal,email_intake,google` enables integrations; in
multi-instance mode `HANDLER_I_<NAME>__INTEGRATIONS` (or
`HANDLER_I_<NAME>__HANDLER_INTEGRATIONS`) overrides it per instance. The
scoped env produced by `server.instance_configs` already has the prefix
stripped, so `INTEGRATIONS` there is the per-instance override. Unset means
"not configured": the instance keeps its legacy wiring.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .base import (
    BackgroundTask,
    Integration,
    IntegrationConfig,
    IntegrationContext,
    PromptFragment,
    SettingsSection,
    ThreadTypeSpec,
    UIManifestEntry,
)

ENV_VAR = "HANDLER_INTEGRATIONS"
SCOPED_ENV_VAR = "INTEGRATIONS"  # what HANDLER_I_<NAME>__INTEGRATIONS becomes


class IntegrationError(ValueError):
    """Startup configuration error; the message names the instance and cause."""


def enabled_names(env: Mapping[str, str]) -> list[str] | None:
    """Integration names enabled for an instance, or None when unconfigured."""
    raw = env.get(SCOPED_ENV_VAR)
    if raw is None:
        raw = env.get(ENV_VAR)
    if raw is None:
        return None
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if len(names) != len(set(names)):
        raise IntegrationError(f"{ENV_VAR}: duplicate integration in {raw!r}")
    return names


@dataclass
class RuntimeContributions:
    routers: list[tuple[str, Any]] = field(default_factory=list)
    tool_sets: list[tuple[str, Any]] = field(default_factory=list)
    sinks: list[tuple[str, Any]] = field(default_factory=list)
    background_tasks: list[tuple[str, BackgroundTask]] = field(default_factory=list)
    identity_provider: tuple[str, Any] | None = None


@dataclass
class LoadedIntegrations:
    """The configured integrations of one instance and their static contributions."""

    instance_name: str
    integrations: list[Integration]
    thread_types: dict[str, tuple[str, ThreadTypeSpec]] = field(default_factory=dict)
    settings_sections: dict[str, tuple[str, SettingsSection]] = field(default_factory=dict)
    prompt_fragments: list[tuple[str, PromptFragment]] = field(default_factory=list)
    ui_entries: list[tuple[str, UIManifestEntry]] = field(default_factory=list)
    configured: bool = True  # False when HANDLER_INTEGRATIONS is unset

    @property
    def names(self) -> list[str]:
        return [integration.name for integration in self.integrations]

    def _where(self) -> str:
        return self.instance_name or "instance"

    def collect_runtime(self, ctx: IntegrationContext, *,
                        reserved_routes: set[tuple[str, str]] = frozenset(),
                        reserved_tools: set[str] = frozenset()) -> RuntimeContributions:
        """Call the runtime hooks and reject duplicate routes, tools and providers."""
        out = RuntimeContributions()
        routes: dict[tuple[str, str], str] = {key: "core" for key in reserved_routes}
        tools: dict[str, str] = {name: "core" for name in reserved_tools}
        sinks: dict[str, str] = {}
        tasks: dict[str, str] = {}
        for integration in self.integrations:
            name = integration.name
            for router in integration.routers(ctx):
                for key in route_keys(router):
                    if key in routes:
                        raise IntegrationError(
                            f"{self._where()}: route {key[0]} {key[1]} from {name!r} "
                            f"duplicates one from {routes[key]!r}")
                    routes[key] = name
                out.routers.append((name, router))
            for tool_set in integration.tool_sets(ctx):
                for spec in tool_specs(tool_set):
                    if spec.name in tools:
                        raise IntegrationError(
                            f"{self._where()}: tool {spec.name!r} from {name!r} "
                            f"duplicates one from {tools[spec.name]!r}")
                    tools[spec.name] = name
                out.tool_sets.append((name, tool_set))
            for sink in integration.sinks(ctx):
                if sink.name in sinks:
                    raise IntegrationError(
                        f"{self._where()}: sink {sink.name!r} from {name!r} "
                        f"duplicates one from {sinks[sink.name]!r}")
                sinks[sink.name] = name
                out.sinks.append((name, sink))
            for task in integration.background_tasks(ctx):
                if task.name in tasks:
                    raise IntegrationError(
                        f"{self._where()}: background task {task.name!r} from {name!r} "
                        f"duplicates one from {tasks[task.name]!r}")
                tasks[task.name] = name
                out.background_tasks.append((name, task))
            provider = integration.identity_provider(ctx)
            if provider is not None:
                if out.identity_provider is not None:
                    raise IntegrationError(
                        f"{self._where()}: only one identity provider is allowed; "
                        f"{out.identity_provider[0]!r} and {name!r} both provide one")
                out.identity_provider = (name, provider)
        return out

    def manifest(self) -> dict[str, Any]:
        return {
            "instance": self.instance_name or "main",
            "configured": self.configured,
            "integrations": [{"name": i.name, "label": i.label or i.name,
                              "requires": list(i.requires)} for i in self.integrations],
            "entries": [entry.to_dict(name) for name, entry in self.ui_entries],
        }


def route_keys(router: Any) -> list[tuple[str, str]]:
    keys = []
    for route in getattr(router, "routes", []):
        path = getattr(route, "path", None)
        if path is None:
            continue
        methods = getattr(route, "methods", None) or {"*"}
        keys.extend((method, path) for method in sorted(methods))
    return keys


def tool_specs(tool_set: Any) -> list[Any]:
    tools = getattr(tool_set, "tools", None)
    return list(tools()) if callable(tools) else [tool_set]


class IntegrationRegistry:
    """Integration classes (or zero-argument factories) keyed by name."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], Integration]] = {}

    def register(self, factory: Callable[[], Integration], name: str | None = None):
        key = name or getattr(factory, "name", "")
        if not key:
            raise ValueError("integration has no name")
        if key in self._factories:
            raise ValueError(f"integration {key!r} is already registered")
        self._factories[key] = factory
        return factory

    def names(self) -> list[str]:
        return sorted(self._factories)

    def load(self, names: list[str], *, instance_name: str = "",
             env: Mapping[str, str] | None = None, base_path: str = "",
             reserved_thread_types: frozenset[str] = frozenset()) -> LoadedIntegrations:
        where = instance_name or "instance"
        unknown = [n for n in names if n not in self._factories]
        if unknown:
            known = ", ".join(self.names()) or "none"
            raise IntegrationError(
                f"{where}: unknown integration(s) {', '.join(unknown)} (known: {known})")
        enabled = set(names)
        for n in names:
            missing = [r for r in getattr(self._factories[n], "requires", ()) if r not in enabled]
            if missing:
                raise IntegrationError(
                    f"{where}: integration {n!r} requires {', '.join(missing)}; "
                    f"add it to {ENV_VAR}")
        config_base = dict(env or {})
        loaded = LoadedIntegrations(instance_name, [])
        for n in names:
            integration = self._factories[n]()
            if integration.name != n:
                raise IntegrationError(f"{where}: integration {n!r} reports name "
                                       f"{integration.name!r}")
            try:
                integration.configure(IntegrationConfig(instance_name, config_base,
                                                        base_path, tuple(names)))
            except IntegrationError:
                raise
            except ValueError as exc:
                raise IntegrationError(f"{where}: integration {n!r}: {exc}") from exc
            loaded.integrations.append(integration)
            for spec in integration.thread_types():
                if spec.name in reserved_thread_types:
                    raise IntegrationError(
                        f"{where}: thread type {spec.name!r} from {n!r} is a core thread type")
                if spec.name in loaded.thread_types:
                    raise IntegrationError(
                        f"{where}: thread type {spec.name!r} from {n!r} duplicates one "
                        f"from {loaded.thread_types[spec.name][0]!r}")
                loaded.thread_types[spec.name] = (n, spec)
            for section in integration.settings_sections():
                if section.key in loaded.settings_sections:
                    raise IntegrationError(
                        f"{where}: settings section {section.key!r} from {n!r} duplicates "
                        f"one from {loaded.settings_sections[section.key][0]!r}")
                loaded.settings_sections[section.key] = (n, section)
            loaded.prompt_fragments.extend((n, f) for f in integration.prompt_fragments())
            loaded.ui_entries.extend((n, e) for e in integration.ui_entries())
        return loaded


# The in-tree registry. Phase 2 registers crontal, email_intake and google here.
default_registry = IntegrationRegistry()
