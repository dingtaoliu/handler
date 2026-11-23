"""The integration interface.

An integration is an external app (Crontal, email intake, Google) that
contributes inputs, outputs, tools, identity, settings, prompt fragments and UI
entries to tenants. Every hook is optional and returns an empty contribution by
default. Integrations provide handlers and factories; the host owns every loop and
the one HTTP app (role-split-design.md, "Integration contract").

Once per host:
- `routers(TenantRoutes)` is called on an instance that is **not configured**: the
  host's one app registers the routes once. A handler finds the request's tenant
  through the `TenantRoutes` it was given (`integration()`, that tenant's configured
  instance; `context()`, its `IntegrationContext`). The routes are served only to
  tenants that enable the integration and whose instance `serves_routes`; any other
  tenant gets a 404.

Per tenant context (built on demand, tenancy/contexts.py):
1. the registry instantiates the class (no arguments);
2. `configure(IntegrationConfig)` reads the tenant's own config and raises
   `ValueError` naming the missing variable when required config is absent;
3. static hooks (`thread_types`, `settings_sections`, `settings_defaults`,
   `prompt_fragments`, `ui_entries`) are collected and checked for duplicates;
4. factories (`tool_sets`, `sinks`, `identity_provider`, `email_intake_observers`,
   `serves_routes`) receive the tenant's `IntegrationContext` and return objects that
   live as long as the context; they start nothing. Then `load(ctx)` is awaited for
   loading work such as tool discovery, and called again before a run while it
   reports it is not done;
5. timer kinds (`timer_kinds`, static): the host's one timer calls a kind's
   handler with the tenant's `IntegrationContext` when one of its due rows is due
   (see TimerKind).

There are no background tasks: time-driven work is a timer kind, and start-up work
belongs in a factory (the Crontal tools are discovered while the context loads).

The interface deliberately avoids importing core internals at runtime (only
under TYPE_CHECKING) so integrations can later move to an entry-point package.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import APIRouter

    from ..tenancy.routing import TenantRoutes

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
    services: Any = None  # the host's shared services (e.g. crontal_discovery)
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


# observer(raw_bytes, *, envelope_to=..., thread_key=...) -> Awaitable
EmailIntakeObserver = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class TimerKind:
    """Time-driven work run by the host's one timer (runtime/timer.py).

    Work is a due row (tenant, kind, ref, due_at) in store/due_work.py. The
    integration writes its rows through the tenant's store, in the same transaction
    as the work where it can: `store.due_statements(kind, ref, due_at)` for a
    caller's own transaction, `store.set_due(kind, ref, due_at | None)` (None
    removes it) and `store.prune_due(kind, keep_refs)`.

    `handler(ctx, item)` does one firing for `item.ref` and returns the next due
    time, or None when nothing more is due (the row is removed). An exception is
    recorded on the row and retried with backoff. `backfill(ctx)` runs whenever the
    tenant starts and must make the kind's rows match the integration's data,
    idempotently (e.g. a row for each enabled mailbox, none for others).
    `timeout_seconds` bounds one firing.
    """

    name: str
    handler: Callable[[IntegrationContext, Any], Awaitable[Any]]
    backfill: Callable[[IntegrationContext], Awaitable[None]] | None = None
    timeout_seconds: float = 120.0


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

    def settings_defaults(self) -> dict[str, dict]:
        """Values for core settings sections before anything is saved there,
        e.g. {"memory": {"kinds": [...]}}. A saved version always wins."""
        return {}

    def ui_entries(self) -> list[UIManifestEntry]:
        return []

    # ---- the host's one app (once per host, not configured) ----------
    def routers(self, tenant: TenantRoutes) -> list[APIRouter]:
        """Routes registered once for the host. Handlers read the request's tenant
        through `tenant`; never read `self`'s config here (it has none)."""
        return []

    # ---- per-tenant factories ---------------------------------------
    def serves_routes(self, ctx: IntegrationContext) -> bool:
        """Whether this tenant is served this integration's routes (else 404)."""
        return True

    def tool_sets(self, ctx: IntegrationContext) -> list[ToolProvider | ToolSpec]:
        return []

    def sinks(self, ctx: IntegrationContext) -> list[Sink]:
        return []

    async def load(self, ctx: IntegrationContext) -> bool:
        """Work while the tenant's context loads (awaited; start no task), e.g. tool
        discovery. Return False to be called again before the tenant's next run (the
        work is not done yet, such as tools a service hasn't listed); an exception is
        logged and the context loads without it."""
        return True

    def identity_provider(self, ctx: IntegrationContext) -> Any | None:
        """Where users come from and how to act as them. At most one per instance."""
        return None

    def timer_kinds(self) -> list[TimerKind]:
        """Kinds of due work this integration handles (static, like thread types)."""
        return []

    def email_intake_observers(self, ctx: IntegrationContext) -> list[EmailIntakeObserver]:
        """Called with each message /ingest/email accepts, after it is queued
        (e.g. to archive it). Failures are logged and never fail the intake."""
        return []
