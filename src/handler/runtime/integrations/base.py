"""The integration interface.

An integration is an external app (Crontal, email intake, Google) that
contributes inputs, outputs, tools, identity, settings, prompt fragments, UI
entries and background tasks to one instance. Every hook is optional and
returns an empty contribution by default.

Lifecycle per instance:
1. the registry instantiates the class (no arguments);
2. `configure(IntegrationConfig)` reads the integration's own namespaced config
   and raises `ValueError` naming the missing variable when required config is
   absent;
3. static hooks (`thread_types`, `settings_sections`, `prompt_fragments`,
   `ui_entries`) are collected and checked for duplicates;
4. runtime hooks (`routers`, `tool_sets`, `sinks`, `identity_provider`,
   `background_tasks`) receive an `IntegrationContext` holding the instance's
   core services.

The interface deliberately avoids importing core internals at runtime (only
under TYPE_CHECKING) so integrations can later move to an entry-point package.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncio

    from fastapi import APIRouter

    from ..gateway.base import ToolProvider, ToolSpec
    from ..sinks import Sink


@dataclass(frozen=True)
class IntegrationConfig:
    """What `configure` sees: this instance's resolved environment only."""

    instance_name: str  # "" for the single-instance (legacy) host, else e.g. "main"
    env: Mapping[str, str]
    base_path: str = ""
    enabled: tuple[str, ...] = ()

    def require(self, key: str) -> str:
        value = (self.env.get(key) or "").strip()
        if not value:
            where = self.instance_name or "instance"
            raise ValueError(f"{where}: {key} is required")
        return value


@dataclass
class IntegrationContext:
    """Core services handed to runtime hooks. Typed loosely on purpose."""

    instance_name: str
    base_path: str
    env: Mapping[str, str]
    store: Any
    bus: Any
    settings: Any
    gateway: Any
    outbox: Any
    notifier: Any
    auth: Any
    workspaces_dir: Any = None
    workspace_sync: Any = None
    file_store: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ThreadTypeSpec:
    """An input thread type. `name` matches `ThreadSpec.type` values."""

    name: str
    lane: str = "background"  # a `types.Lane` value
    visibility: str = "shared"  # "shared" (all users) or "owner"
    acts_as: str = "instance"  # whose identity runs act as: "instance", "owner", "sender"
    default_instructions: str = ""
    label: str = ""
    render_turn: Callable[..., str] | None = None


@dataclass(frozen=True)
class SettingsSection:
    """A settings section owned by an integration (pydantic model optional)."""

    key: str
    label: str = ""
    model: Any = None
    defaults: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptFragment:
    """Text appended to the persona/instructions, optionally per thread type."""

    key: str
    text: str
    thread_types: tuple[str, ...] = ()  # empty = every thread type


@dataclass(frozen=True)
class UIManifestEntry:
    """One UI element listed by `GET /api/ui/manifest`."""

    kind: str  # "nav" | "settings_section" | "thread_type" | "connection"
    id: str
    label: str = ""
    path: str = ""
    props: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self, integration: str) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "label": self.label or self.id,
                "path": self.path, "props": dict(self.props), "integration": integration}


@dataclass(frozen=True)
class BackgroundTask:
    """A long-running coroutine started under the instance lifespan.

    `run(stop)` must return soon after `stop` is set.
    """

    name: str
    run: Callable[[asyncio.Event], Awaitable[None]]


class Integration:
    """Base class; override `name` and the hooks you need."""

    name: ClassVar[str] = ""
    requires: ClassVar[tuple[str, ...]] = ()
    label: ClassVar[str] = ""

    def configure(self, config: IntegrationConfig) -> None:
        self.config = config

    # ---- static contributions ---------------------------------------
    def thread_types(self) -> list[ThreadTypeSpec]:
        return []

    def settings_sections(self) -> list[SettingsSection]:
        return []

    def prompt_fragments(self) -> list[PromptFragment]:
        return []

    def ui_entries(self) -> list[UIManifestEntry]:
        return []

    # ---- runtime contributions --------------------------------------
    def routers(self, ctx: IntegrationContext) -> list[APIRouter]:
        return []

    def tool_sets(self, ctx: IntegrationContext) -> list[ToolProvider | ToolSpec]:
        return []

    def sinks(self, ctx: IntegrationContext) -> list[Sink]:
        return []

    def identity_provider(self, ctx: IntegrationContext) -> Any | None:
        """Where users come from and how to act as them. At most one per instance."""
        return None

    def background_tasks(self, ctx: IntegrationContext) -> list[BackgroundTask]:
        return []
