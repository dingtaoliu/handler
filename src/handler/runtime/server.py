"""`handler-rt serve`: the runtime in one process.

Web API + MCP gateway + dispatcher + scheduler, sharing one store.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import ipaddress
import json
import logging
import os
import re
import secrets
import shlex
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pathlib import Path

import uvicorn

from .auth import Authenticator, Principal, add_cli_commands, run_cli
from .bus import RunEventBus
from . import main_limits
from .context import TenantScope, reset_current_tenant, set_current_tenant
from .documents import configure_readers, shutdown_readers
from .files import LocalFileStore, S3FileStore
from .gateway.api import approvals_router, questions_router
from .web.memory_recovery_api import memory_recovery_router
from .gateway.questions import QuestionService, QuestionTools
from .gateway.gateway import Gateway
from .gateway.gmail import GmailTools
from .gateway.google_oauth import INSTANCE_OWNER, google_oauth_from_env, user_owner
from .gateway.mcp_server import create_mcp_app
from .gateway.policy import GrantPolicy
from .gateway.secret_box import SecretBox
from .gateway.tokens import RunTokens
from .harness.codex import SANDBOX_MODES, CodexHarness, codex_binary
from .gateway.crontal_api import CrontalApiTools, crontal_base_url
from .harness.openai_agents import OpenAIAgentsHarness
from .integrations.crontal.connector import run_crontal_connector
from .integrations.crontal.sink import crontal_sink_from_config
from .integrations.base import IntegrationContext, TimerKind
from .integrations.registry import (
    IntegrationRegistry,
    LoadedIntegrations,
    default_registry,
    enabled_names,
    route_keys,
)
from .memory import MemoryTools
from .notifications import Notifier
from .observability.logging import configure_json_logging
from .observability.pricing import PriceTable
from .run import RunConfig, RunExecutor, log_run_failed, thread_slug
from .retention import retention_due
from .scheduler import Scheduler
from .settings import (
    ALLOWED_HARNESSES_ENV,
    HarnessName,
    InstanceDefaults,
    SettingsService,
    check_harness,
    parse_allowed_harnesses,
)
from .sinks import HostOutbox, Outbox, outbox_settings
from .store.postgres import PostgresStore
from .store.postgres_hub import DEFAULT_POOL_MAX, PostgresHub
from .store.sqlite import SQLiteStore
from .tasks import publish_queued
from .store.due_work import (
    APPROVAL_EXPIRE,
    INPUTS_RELEASE,
    QUESTION_EXPIRE,
    RETENTION_CLEANUP,
    SCHEDULE_FIRE,
)
from .timer import HostTimer, TimerTarget, timer_settings
from .runner import HostRunner, runner_settings
from .storage import register_workspace_limit, unregister_workspace_limit
from .tenancy.contexts import (
    DEFAULT_CACHE_SIZE,
    DEFAULT_CACHE_TTL_SECONDS,
    ContextCache,
    HostTenants,
    ServedTenant,
    TenantContext,
)
from .tenancy.host import PINNED_INTEGRATIONS, EnvRouter, TenantHost
from .tenancy.limits import (
    PLAN_LIMITS_ENV,
    LiveLimits,
    PlanLimits,
    check_limit_keys,
    parse_plan_table,
    resolve_limits,
    upgrade_required,
)
from .tenancy.platform_api import platform_router
from .tenancy.plan_guard import ConsoleGuard
from .tenancy.registry import Tenant, TenantRegistry
from .tenancy.run_gate import DEFAULT_MAX_RUNS, DEFAULT_PLAN_CAPS, RunGate, parse_plan_caps
from .tools.automation import AutomationTools
from .tools.notify import NotifyTools
from .tools.threads import ThreadTools
from .types import Event, Lane, ThreadSpec
from .uploads import UploadStore
from .web.admin_api import admin_router
from .web.usage_api import usage_router
from .web.automation_dashboard import automation_dashboard_router
from .web.app import create_app
from .web.email_ingest import email_ingest_router
from .web.google_api import google_router
from .web.ingest import crontal_ingest_router
from .web.settings_api import settings_router
from .web.tasks_api import tasks_router
from .web.ui_manifest_api import ui_manifest_router
from .workspaces import WorkspaceBackstop, WorkspaceSync

logger = logging.getLogger("handler.runtime.server")

FULL_GOOGLE_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
)


def thread_router(store: SQLiteStore | PostgresStore):
    """Route an event (e.g. approval.resolved) into its existing thread."""

    async def emit(event: Event) -> None:
        thread = await store.get_thread(event.thread_key) if event.thread_key else None
        spec = None
        if thread is not None:
            spec = ThreadSpec(key=thread.key, type=thread.type, lane=thread.lane,
                              owner_user_id=thread.owner_user_id, harness=thread.harness,
                              model=thread.model, priority=thread.priority)
        await store.put(event, spec)

    return emit


def read_env_file(path: str | Path) -> dict[str, str]:
    """Read a harness env file without changing the host process environment."""
    result = {}
    for line in Path(path).expanduser().read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def _write_env_values(path: Path, values: Mapping[str, str]) -> None:
    """Replace selected shell env assignments atomically without exposing values."""
    path = path.expanduser()
    lines = path.read_text().splitlines() if path.exists() else []
    pending = dict(values)
    output = []
    assignment = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")
    for line in lines:
        match = assignment.match(line)
        key = match.group(1) if match else None
        if key not in values:
            output.append(line)
        elif key in pending:
            output.append(f"{key}={shlex.quote(pending.pop(key))}")
    if output and output[-1]:
        output.append("")
    output.extend(f"{key}={shlex.quote(value)}" for key, value in pending.items())
    payload = ("\n".join(output) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _configure_google(args: argparse.Namespace) -> None:
    path = Path(args.env_file).expanduser()
    current = read_env_file(path) if path.exists() else {}
    current_id = current.get("GOOGLE_OAUTH_CLIENT_ID", "")
    label = f" [{current_id}]" if current_id else ""
    client_id = input(f"Google OAuth client ID{label}: ").strip() or current_id
    secret_prompt = "Google OAuth client secret"
    if current.get("GOOGLE_OAUTH_CLIENT_SECRET"):
        secret_prompt += " (Enter keeps the current secret)"
    client_secret = getpass.getpass(secret_prompt + ": ").strip()
    client_secret = client_secret or current.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise SystemExit("Both the Google OAuth client ID and client secret are required.")
    secret_key = current.get("HANDLER_SECRET_KEY") or secrets.token_urlsafe(48)
    _write_env_values(path, {
        "GOOGLE_OAUTH_CLIENT_ID": client_id,
        "GOOGLE_OAUTH_CLIENT_SECRET": client_secret,
        "HANDLER_PUBLIC_URL": args.public_url.rstrip("/"),
        "HANDLER_SECRET_KEY": secret_key,
        "GOOGLE_OAUTH_SCOPES": " ".join(FULL_GOOGLE_SCOPES),
    })
    print(f"Google OAuth configuration saved to {path} (mode 600).")
    print("Restart Handler, open an SSH tunnel, then connect Google in Connections.")


def load_env_file(path: str | Path) -> dict[str, str]:
    """Load KEY=VALUE lines into os.environ (existing variables win).

    Codex inherits the process environment, so this is how a deployment hands
    secrets such as CODEX_API_KEY to the harness from a file.
    """
    loaded: dict[str, str] = {}
    for line in Path(path).expanduser().read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def _log_file(args: argparse.Namespace) -> Path:
    if args.log_file:
        return Path(args.log_file).expanduser()
    return Path(args.data_dir).expanduser() / "logs" / "handler-rt.jsonl"


def _settings_defaults(integrations: Any, legacy_crontal: bool) -> dict[str, dict]:
    """Integration-provided values for settings sections nothing was saved to."""
    out = {section: value for section, (_, value) in integrations.settings_defaults.items()}
    if legacy_crontal and "memory" not in out:
        from .integrations.crontal.memory_kinds import CRONTAL_MEMORY_DEFAULTS

        out["memory"] = CRONTAL_MEMORY_DEFAULTS
    return out


def _crontal_url_and_token(args: Any, env: Any, integrations: Any,
                           legacy: bool) -> tuple[str | None, str | None]:
    url, token = None, None
    if legacy:
        url, token = args.crontal_url, env.get("CRONTAL_AGENT_TOKEN")
    else:
        for integration in integrations.integrations:
            if integration.name == "crontal":
                url = getattr(integration, "crontal_url", None)
                token = getattr(integration, "agent_token", None)
    return url, token


def _crontal_identity_client(args: Any, env: Any, integrations: Any, legacy: bool) -> Any:
    """The instance's Crontal AI identity client (shared with the Crontal tools),
    or None when this instance has no Crontal URL and agent token."""
    from .gateway.crontal_identity import crontal_identity

    url, token = _crontal_url_and_token(args, env, integrations, legacy)
    if not (url and token):
        return None
    return crontal_identity(crontal_base_url(url), token)


def _crontal_route_catalog(args: Any, env: Any, integrations: Any, legacy: bool) -> Any:
    """The instance's Crontal API catalog (shared with the crontal_api tools),
    or None when this instance has no Crontal URL and agent token."""
    from .gateway.crontal_catalog import crontal_route_catalog

    url, token = _crontal_url_and_token(args, env, integrations, legacy)
    if not (url and token):
        return None
    return crontal_route_catalog(crontal_base_url(url), token)


def _trusted_device_users(raw: str) -> dict[str, str]:
    """Parse exact Tailscale IP=user mappings; unknown peers remain unauthenticated."""
    result = {}
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        address, separator, user_id = item.partition("=")
        address, user_id = address.strip(), user_id.strip()
        if not separator or not user_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]*", user_id):
            raise ValueError(f"invalid HANDLER_TRUSTED_DEVICE_USERS entry: {item!r}")
        try:
            normalized = str(ipaddress.ip_address(address))
        except ValueError as exc:
            raise ValueError(f"invalid trusted device IP: {address!r}") from exc
        if normalized in result:
            raise ValueError(f"duplicate trusted device IP: {normalized}")
        result[normalized] = user_id
    return result


def _parse_args(argv: list[str] | None, env: Mapping[str, str] | None = None) -> argparse.Namespace:
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(prog="handler-rt", description="Handler runtime (rebuild)")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve", help="run the web API, MCP gateway, dispatcher and scheduler")
    s.add_argument("--data-dir", default=env.get("HANDLER_RT_DATA_DIR", "~/.handler-rt"))
    s.add_argument("--database-url", default=env.get("DATABASE_URL"),
                   help="Postgres URL; when set, use PostgresStore instead of SQLite")
    s.add_argument("--db-schema", default=env.get("HANDLER_DB_SCHEMA"),
                   help="Postgres schema for all tables (created if missing)")
    s.add_argument("--harness-env-file", default=env.get("HANDLER_HARNESS_ENV_FILE"),
                   help="KEY=VALUE file loaded into the environment the harness inherits")
    s.add_argument("--host", default=env.get("HANDLER_RT_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(env.get("PORT", 8100)))
    s.add_argument("--file-store", choices=("local", "s3"),
                   default=env.get("HANDLER_FILE_STORE", "local"),
                   help="workspace backups: local data-dir/files or Bucketeer S3")
    s.add_argument("--model", default=env.get("HANDLER_RT_MODEL"),
                   help="default model when the thread-type settings don't set one")
    s.add_argument("--reasoning-effort", default=env.get("HANDLER_RT_EFFORT", "low"))
    s.add_argument("--codex-auth-json", default=env.get("HANDLER_RT_CODEX_AUTH"),
                   help="auth.json copied into each isolated CODEX_HOME (dev only)")
    s.add_argument("--handler-yaml", default=None, help="seed settings from this file once")
    s.add_argument("--agent-md", default=None, help="seed persona from this AGENT.md once")
    s.add_argument("--users", default=env.get("HANDLER_RT_USERS", ""),
                   help="comma-separated user ids (for notify to 'all')")
    s.add_argument("--max-concurrent", type=int,
                   default=int(env["HANDLER_MAX_CONCURRENT"])
                   if env.get("HANDLER_MAX_CONCURRENT") else None,
                   help="maximum active runs across all lanes")
    s.add_argument("--codex-auth-link", action="store_true",
                   default=env.get("HANDLER_RT_CODEX_AUTH_LINK", "") == "1",
                   help="symlink --codex-auth-json into each harness home instead of "
                        "copying it (keeps one shared ChatGPT login fresh)")
    s.add_argument("--codex-sandbox",
                   default=env.get("HANDLER_CODEX_SANDBOX", "workspace-write"),
                   choices=SANDBOX_MODES,
                   help="Codex sandbox mode; use danger-full-access only where the container "
                        "itself is the isolation boundary (e.g. Heroku dynos)")
    s.add_argument("--sync-interval", type=float,
                   default=float(env.get("HANDLER_SYNC_INTERVAL_SECONDS", "300")),
                   help="seconds between background workspace uploads (S3 file store)")
    s.add_argument("--default-harness", default=env.get("HANDLER_DEFAULT_HARNESS"),
                   choices=[h.value for h in HarnessName],
                   help="harness for every thread type on a fresh instance "
                        "(later changes go through settings)")
    s.add_argument("--crontal-url", default=env.get("CRONTAL_URL"),
                   help="Crontal MCP endpoint, e.g. https://crontal.ai/api/v1/agent-mcp "
                        "(token from $CRONTAL_AGENT_TOKEN)")
    s.add_argument("--admin-key", default=env.get("HANDLER_ADMIN_KEY"),
                   help="instance admin key (bearer credential with admin rights)")
    s.add_argument("--log-level", default="INFO")
    s.add_argument("--log-file", default=env.get("HANDLER_RT_LOG_FILE"),
                   help="rotating JSON-lines log file (default <data-dir>/logs/handler-rt.jsonl)")
    add_cli_commands(sub)
    imp = sub.add_parser("import-legacy",
                         help="copy persona, memory, users and chats from a legacy handler")
    imp.add_argument("--from", dest="source", required=True,
                     help="legacy instance dir, e.g. ~/.handler/instances/personal")
    imp.add_argument("--data-dir", default=env.get("HANDLER_RT_DATA_DIR", "~/.handler-rt"))
    imp.add_argument("--default-user", default="danny",
                     help="owner for legacy data without a user id")
    google = sub.add_parser("google", help="configure Google OAuth")
    google_sub = google.add_subparsers(dest="google_command", required=True)
    configure = google_sub.add_parser(
        "configure", help="interactively configure full Gmail and Workspace access")
    configure.add_argument("--env-file", default="~/.handler-rt/env")
    configure.add_argument("--public-url", default="http://localhost:8100")
    return p.parse_args(argv)


# Additional instances inherit infrastructure and harness defaults, but no account
# credentials or integration destinations. Main keeps its existing deployment env.
_PRIVATE_ENV = {
    "HANDLER_ADMIN_KEY", "HANDLER_WEBHOOK_SECRET", "HANDLER_EMAIL_SECRET",
    "HANDLER_SECRET_KEY", "HANDLER_PUBLIC_URL", "HANDLER_RETURN_TO_ORIGINS",
    "HANDLER_TRUSTED_USER", "HANDLER_TRUSTED_DEVICE_USERS",
    "HANDLER_HARNESS_ENV_FILE", "HANDLER_RT_CODEX_AUTH", "HANDLER_RT_USERS",
    "HANDLER_RT_LOG_FILE", "CRONTAL_URL", "CRONTAL_AGENT_TOKEN",
    "HANDLER_INTEGRATIONS",  # each instance chooses its own integrations
}

# Registry-mode settings of the host itself: no tenant, main included, ever sees them.
REGISTRY_ENV = "HANDLER_TENANT_REGISTRY"
PLATFORM_KEY_ENV = "HANDLER_PLATFORM_KEY"
TENANT_SECRET_ENV = "HANDLER_TENANT_SECRET_KEY"
# HANDLER_ALLOWED_HARNESSES is read once from the host's own environment and
# applies to every instance and tenant; none can widen it.
_HOST_ONLY_ENV = {REGISTRY_ENV, PLATFORM_KEY_ENV, TENANT_SECRET_ENV, "HANDLER_INSTANCES",
                  ALLOWED_HARNESSES_ENV}

# Thread types the core owns; integrations may not redefine them.
CORE_THREAD_TYPES = frozenset({"chat", "automation"})


# Time-driven work the core owns (store/due_work.py); `context` is the instance's
# lifecycle namespace. Integrations add theirs (Integration.timer_kinds).
CORE_TIMER_KINDS = (
    TimerKind(SCHEDULE_FIRE, lambda c, item: c.scheduler.fire_due(item.ref)),
    TimerKind(APPROVAL_EXPIRE, lambda c, item: c.gateway.expire_due(item.ref)),
    TimerKind(QUESTION_EXPIRE, lambda c, item: c.questions.expire_due(item.ref)),
    TimerKind(INPUTS_RELEASE, lambda c, item: c.store.release_held_due()),
    TimerKind(RETENTION_CLEANUP, lambda c, item: retention_due(c.store), timeout_seconds=600),
)


class HostTimerDirectory:
    """Tenants as the host timer sees them: every enabled tenant's due rows, the
    tenant resolved through the context cache for the handler's duration (pinned, so
    it can't be closed under the firing). Hub tenants' rows are in the control schema;
    a pinned local tenant has its own queue."""

    def __init__(self, tenants: HostTenants) -> None:
        self.tenants = tenants
        self._queues: dict[int, Any] = {}  # id(queue) -> hub, or the local tenant's name

    def sources(self) -> list[tuple[Any, list[str]]]:
        out = []
        enabled = self.tenants.enabled()
        for hub in self.tenants.hubs():
            queue = hub.due_queue()
            self._queues[id(queue)] = hub
            keys = [tenant.key for tenant in enabled if tenant.hub is hub]
            if keys:
                out.append((queue, keys))
        for tenant in enabled:
            if tenant.hub is not None:
                continue
            context = self.tenants.cache.cached(tenant.name)
            if context is not None and context.timer_target is not None:
                queue = context.timer_target.queue
                self._queues[id(queue)] = tenant.name
                out.append((queue, [context.key]))
        return out

    @contextlib.asynccontextmanager
    async def tenant(self, queue: Any, key: str):
        owner = self._queues.get(id(queue))
        if isinstance(owner, str):
            name = owner
        else:
            tenant = self.tenants.by_key(owner, key) if owner is not None else None
            name = tenant.name if tenant is not None else None
        if name is None:
            yield None
            return
        async with self.tenants.use(name) as context:
            yield None if context is None else context.timer_target


class HostServices:
    """What every tenant on the host shares (role-split-design.md, steps 2 and 3): one
    bus, one MCP app with host-wide run tokens, the tenant context cache, and the
    host loops: the timer over due work, the claim loop over due threads, the outbox
    loop over due deliveries, and the workspace backstop. No tenant has a task.

    A lone `instance_runtime` with none given makes its own: the same code path, one
    tenant."""

    def __init__(self, env: Mapping[str, str], directory: Any = None, *,
                 sync_interval: float = 300.0, gate: RunGate | None = None) -> None:
        self.bus = RunEventBus()
        self.tokens = RunTokens()
        self.mcp = create_mcp_app(None, self.tokens)
        self.cache = ContextCache(
            max_size=int(env.get("HANDLER_TENANT_CACHE_SIZE", str(DEFAULT_CACHE_SIZE))),
            ttl_seconds=float(env.get("HANDLER_TENANT_CACHE_TTL_SECONDS",
                                      str(DEFAULT_CACHE_TTL_SECONDS))))
        self.tenants = HostTenants(self.cache)
        self.gate = gate
        self.runner = HostRunner(self.tenants, gate, **runner_settings(env))
        self.outbox = HostOutbox(self.tenants, **outbox_settings(env))
        self.directory = directory if directory is not None else HostTimerDirectory(self.tenants)
        self.timer = HostTimer(self.directory, **timer_settings(env))
        self.workspaces = WorkspaceBackstop(sync_interval)
        self.on_tenant_changed: list = []  # e.g. the registry host re-reads the row
        self._stop: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []

    def attach_hub(self, hub: PostgresHub) -> None:
        """One LISTEN for the whole host: due work wakes the timer, a schema's work the
        claim and outbox loops, a tenant change drops its context."""
        hub.on_due = self.timer.wake

        def work(schema: str) -> None:
            self.runner.hint_schema(hub, schema)
            tenant = self.tenants.by_key(hub, schema) if schema else None
            self.outbox.hint(tenant.name if tenant is not None else "")

        def changed(schema: str) -> None:
            self.tenants.changed(hub, schema)
            for listener in self.on_tenant_changed:
                listener(hub, schema)

        hub.on_work = work
        hub.on_tenant_changed = changed

    def serve_local(self, context: TenantContext) -> None:
        """A tenant on a store with its own connections: only this process hears it, so
        its context stays built (pinned) for the host's lifetime."""
        self.cache.pin(context)
        clock = getattr(context.store, "_clock", None)
        self.tenants.serve(ServedTenant(context.name, context.key, clock=clock))
        self.runner.local.register(context.name, clock)
        self.outbox.local.register(context.name, clock)

    def wire(self, name: str, store: Any, outbox: Any) -> None:
        """A built context's in-process signals go to the host loops."""
        signal = getattr(store, "_wake", None)
        if hasattr(signal, "listener"):
            signal.listener = lambda: self.runner.hint(name)
        store.on_delivery_due = lambda: self.outbox.hint(name)
        outbox.on_wake = lambda: self.outbox.hint(name)

    def start(self) -> None:
        self._stop = asyncio.Event()
        self._mcp_stop = asyncio.Event()
        stop, mcp_stop = self._stop, self._mcp_stop

        async def own_mcp() -> None:
            # MCP uses AnyIO cancel scopes: enter AND exit its lifespan in one task.
            async with self.mcp.lifespan():
                await mcp_stop.wait()

        self._mcp_task = asyncio.create_task(own_mcp(), name="host-mcp")
        self._tasks = [asyncio.create_task(self.timer.run(stop), name="host-timer"),
                       asyncio.create_task(self.runner.run(stop), name="host-runner"),
                       asyncio.create_task(self.outbox.run(stop), name="host-outbox"),
                       asyncio.create_task(self.workspaces.run(stop),
                                           name="workspace-backstop")]

    async def stop(self) -> None:
        if self._stop is None:
            return
        self._stop.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)  # runs drained first
        self._mcp_stop.set()
        await asyncio.gather(self._mcp_task, return_exceptions=True)
        self._stop = None


def instance_configs(args: argparse.Namespace, env: Mapping[str, str]):
    """Resolve immutable instance inputs without ever rebinding process env."""
    raw = env.get("HANDLER_INSTANCES", "").strip()
    if not raw:
        return [("", args, dict(env), "")]
    names = [name.strip() for name in raw.split(",")]
    if not names or len(names) != len(set(names)) or "main" not in names:
        raise ValueError("HANDLER_INSTANCES must contain main and unique instance names")
    configs = []
    for name in names:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
            raise ValueError(f"invalid instance name: {name!r}")
        scoped = {k: v for k, v in env.items() if not k.startswith("HANDLER_I_")}
        if name != "main":
            scoped = {k: v for k, v in scoped.items()
                      if k not in _PRIVATE_ENV and not k.startswith("GOOGLE_")}
            scoped["HANDLER_RT_DATA_DIR"] = str(Path(args.data_dir).expanduser() / "instances" / name)
            scoped["HANDLER_DB_SCHEMA"] = f"handler_{name}"
            scoped["HANDLER_FILE_PREFIX"] = f"handler/{name}/"
        prefix = f"HANDLER_I_{name.upper()}__"
        scoped.update({k[len(prefix):]: v for k, v in env.items() if k.startswith(prefix)})
        instance_args = _parse_args(["serve"], scoped)
        if name == "main":
            # Explicit CLI arguments still take precedence for the main instance.
            for key, value in vars(args).items():
                if value != getattr(_parse_args(["serve"], env), key):
                    setattr(instance_args, key, value)
        instance_args.host, instance_args.port = args.host, args.port
        if not instance_args.admin_key:
            raise ValueError(f"{name}: HANDLER_ADMIN_KEY is required in multi-instance mode")
        base = f"/i/{name}"
        configs.append((name, instance_args, scoped, base))
    validate_instance_configs(configs)
    return configs


def validate_instance_configs(configs: list) -> None:
    """Reject instances that would share a directory, key, schema or file prefix."""
    for index, (name, cfg, scoped, _) in enumerate(configs):
        for other_name, other, other_env, _ in configs[:index]:
            if Path(cfg.data_dir).expanduser().resolve() == Path(other.data_dir).expanduser().resolve():
                raise ValueError(f"{name} and {other_name}: data directories must differ")
            if cfg.admin_key == other.admin_key:
                raise ValueError(f"{name} and {other_name}: admin keys must differ")
            if cfg.database_url and cfg.database_url == other.database_url and (cfg.db_schema or "public") == (other.db_schema or "public"):
                raise ValueError(f"{name} and {other_name}: database schemas must differ")
            if cfg.file_store == other.file_store == "s3":
                left, right = scoped.get("HANDLER_FILE_PREFIX", "").strip("/"), other_env.get("HANDLER_FILE_PREFIX", "").strip("/")
                if not left or not right or left == right or left.startswith(right + "/") or right.startswith(left + "/"):
                    raise ValueError(f"{name} and {other_name}: S3 prefixes must be disjoint")
            for key in ("HANDLER_WEBHOOK_SECRET", "HANDLER_EMAIL_SECRET", "HANDLER_SECRET_KEY"):
                if scoped.get(key) and scoped.get(key) == other_env.get(key):
                    raise ValueError(f"{name} and {other_name}: {key} must differ")


def _shared_hubs(configs: list, env: Mapping[str, str]) -> dict[str, PostgresHub]:
    """One shared pool and LISTEN per database when hosting named instances.

    An unnamed single instance keeps its own connections. HANDLER_DB_POOL_MAX is not
    read here: production still sets it per instance for the previous image to use.
    """
    if not configs[0][0]:
        return {}
    size = int(env.get("HANDLER_DB_SHARED_POOL_MAX", str(DEFAULT_POOL_MAX)))
    return {cfg.database_url: PostgresHub(cfg.database_url, max_size=size)
            for _, cfg, _, _ in configs if cfg.database_url}


def _inheritable_env(env: Mapping[str, str]) -> dict[str, str]:
    """What every registry tenant starts from: infrastructure, never a credential."""
    return {k: v for k, v in env.items()
            if not k.startswith(("HANDLER_I_", "GOOGLE_"))
            and k not in _PRIVATE_ENV and k not in _HOST_ONLY_ENV}


_SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|GOOGLE_")


def tenant_instance_config(args: argparse.Namespace, env: Mapping[str, str], tenant: Tenant,
                           box: SecretBox) -> tuple[str, argparse.Namespace, dict[str, str], str]:
    """A tenant row as instance_configs would resolve it, from its own secrets only."""
    scoped = _inheritable_env(env)
    root = Path(args.data_dir).expanduser()
    scoped["HANDLER_RT_DATA_DIR"] = str(root if tenant.name == "main"
                                        else root / "instances" / tenant.name)
    scoped["HANDLER_DB_SCHEMA"] = tenant.schema_name
    scoped["HANDLER_FILE_PREFIX"] = tenant.s3_prefix
    scoped.pop("INTEGRATIONS", None)
    if tenant.integrations is not None:
        scoped["HANDLER_INTEGRATIONS"] = ",".join(tenant.integrations)
    scoped.update(tenant.config)
    scoped.update(json.loads(box.decrypt(tenant.secrets)))
    tenant_args = _parse_args(["serve"], scoped)
    tenant_args.host, tenant_args.port = args.host, args.port
    tenant_args.database_url = args.database_url
    if not tenant_args.admin_key:
        raise ValueError(f"{tenant.name}: HANDLER_ADMIN_KEY is required")
    return tenant.name, tenant_args, scoped, f"/i/{tenant.name}"


def _imported_tenants(args: argparse.Namespace, env: Mapping[str, str], configs: list,
                      registry: TenantRegistry) -> list[Tenant]:
    """The env-defined instances as tenants, keeping their schemas, prefixes and URLs."""
    if not configs[0][0]:
        return []
    common = _inheritable_env(env)
    root = Path(args.data_dir).expanduser()
    fixed = {"HANDLER_DB_SCHEMA", "HANDLER_FILE_PREFIX", "HANDLER_INTEGRATIONS", "INTEGRATIONS",
             "HANDLER_RT_DATA_DIR", "HANDLER_MERCHANT_ID", *_HOST_ONLY_ENV}
    tenants = []
    for name, cfg, scoped, _ in configs:
        if cfg.database_url != args.database_url:
            raise ValueError(f"{name}: the tenant registry needs one database for every instance")
        overrides = {k: v for k, v in scoped.items() if common.get(k) != v and k not in fixed}
        derived = root if name == "main" else root / "instances" / name
        if Path(cfg.data_dir).expanduser() != derived:
            overrides["HANDLER_RT_DATA_DIR"] = cfg.data_dir
        secrets = {k: v for k, v in overrides.items() if _SECRET_NAME.search(k)}
        merchant = scoped.get("HANDLER_MERCHANT_ID")
        tenants.append(Tenant(
            name=name, merchant_id=int(merchant) if merchant else None,
            schema_name=cfg.db_schema or "public", s3_prefix=scoped.get("HANDLER_FILE_PREFIX", ""),
            integrations=enabled_names(scoped), secrets=registry.seal(secrets),
            config={k: v for k, v in overrides.items() if k not in secrets}))
    return tenants


def _plan_limit(tenant: Tenant) -> int | None:
    value = tenant.limits.get("max_runs")
    return int(value) if value is not None else None


def _host_run_gate(env: Mapping[str, str]) -> RunGate:
    """The one run gate every tenant's dispatcher shares, registry on or off."""
    return RunGate(int(env.get("HANDLER_GLOBAL_MAX_RUNS", str(DEFAULT_MAX_RUNS))),
                   parse_plan_caps(env.get("HANDLER_PLAN_RUN_CAPS", DEFAULT_PLAN_CAPS)),
                   max_wait=float(env.get("HANDLER_RUN_MAX_WAIT_SECONDS", "60")))


def create_registry_host(args: argparse.Namespace, env: dict[str, str], configs: list) -> FastAPI:
    """Host tenants from `handler_control`; each is context, built when it has work."""
    platform_key, sealing_key = env.get(PLATFORM_KEY_ENV), env.get(TENANT_SECRET_ENV)
    if not args.database_url or not platform_key or not sealing_key:
        raise ValueError(f"{REGISTRY_ENV} needs DATABASE_URL, {PLATFORM_KEY_ENV} "
                         f"and {TENANT_SECRET_ENV}")
    size = int(env.get("HANDLER_DB_SHARED_POOL_MAX", str(DEFAULT_POOL_MAX)))
    hub = PostgresHub(args.database_url, max_size=size)
    box = SecretBox(sealing_key)
    gate = _host_run_gate(env)

    plan_table = parse_plan_table(env.get(PLAN_LIMITS_ENV))
    allowed_harnesses = parse_allowed_harnesses(env.get(ALLOWED_HARNESSES_ENV))

    def validate(tenants: list[Tenant]) -> None:
        for tenant in tenants:
            check_limit_keys(tenant.limits, f"{tenant.name}: limits")
            limits = resolve_limits(tenant.plan, tenant.limits, plan_table)
            enabled = set(tenant.integrations or ())
            if ((not limits.connected_mail and PINNED_INTEGRATIONS & enabled)
                    or (not limits.email and "email_intake" in enabled)):
                raise ValueError(f"{tenant.name}: email channels are not included in the "
                                 f"{tenant.plan} plan")
        names = [tenant.name for tenant in tenants]
        if len(names) != len(set(names)):
            raise ValueError("tenant names must be unique")
        schemas = [tenant.schema_name for tenant in tenants]
        if len(schemas) != len(set(schemas)):
            raise ValueError("tenant schemas must differ")
        resolved = [tenant_instance_config(args, env, tenant, box) for tenant in tenants]
        for name, cfg, _, _ in resolved:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
                raise ValueError(f"invalid instance name: {name!r}")
            if cfg.admin_key == platform_key:
                raise ValueError(f"{name}: an admin key must differ from the platform key")
            check_harness(cfg.default_harness, allowed_harnesses,
                          f"{name}: HANDLER_DEFAULT_HARNESS")
        validate_instance_configs(resolved)

    registry = TenantRegistry(hub, box, validate)

    def limits_for(tenant: Tenant) -> PlanLimits:
        gate.configure(tenant.name, tenant.plan, _plan_limit(tenant))
        return resolve_limits(tenant.plan, tenant.limits, plan_table)

    services = HostServices(env, sync_interval=args.sync_interval, gate=gate)
    services.attach_hub(hub)

    async def build(name: str) -> TenantContext:
        # Read afresh: a context is built from the row as it is now (new secrets,
        # plan and integrations apply from the next build).
        tenant = await registry.get(name)
        if tenant is None or not tenant.enabled:
            raise LookupError(f"tenant {name!r} is not enabled")
        name, cfg, scoped, base = tenant_instance_config(args, env, tenant, box)
        return await build_tenant_context(
            cfg, scoped, base_path=base, instance_name=name, hub=hub,
            plan_limits=LiveLimits(limits_for(tenant)),
            allowed_harnesses=allowed_harnesses, host=services)

    services.cache.build = build
    tenants = TenantHost(registry, services, hub, limits_for=limits_for,
                         scan_seconds=float(env.get("HANDLER_TENANT_SCAN_SECONDS", "15")))

    @contextlib.asynccontextmanager
    async def lifespan(host):
        try:
            await registry.setup()
            await registry.import_once(_imported_tenants(args, env, configs, registry))
            validate(await registry.list())
            await tenants.load()
            await tenants.boot()  # every tenant due once: backfill, work left by a restart
            tenants.start()
            services.start()
            yield
        finally:
            await services.stop()
            await tenants.shutdown()
            await services.cache.close()
            await hub.close()
            await asyncio.to_thread(shutdown_readers)

    host = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    host.include_router(platform_router(registry, tenants, platform_key,
                                        allowed_harnesses=allowed_harnesses))
    host.mount("/", tenants)
    host.state.tenants, host.state.registry, host.state.run_gate = tenants, registry, gate
    host.state.services = services
    return host


def create_host(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> FastAPI:
    env = dict(os.environ if env is None else env)
    configs = instance_configs(args, env)
    configure_readers(env)
    main_limits.configure(env)
    if env.get(REGISTRY_ENV) == "1":
        return create_registry_host(args, env, configs)
    allowed_harnesses = parse_allowed_harnesses(env.get(ALLOWED_HARNESSES_ENV))
    hubs = _shared_hubs(configs, env)
    # One run cap for the whole process, as on the registry host. Env-defined
    # instances are paid with no max_runs override: the plan and limits the
    # registry gives them when it imports them (Tenant's defaults).
    gate = _host_run_gate(env)
    for name, *_ in configs:
        gate.configure(name or "main", Tenant.plan, None)
    services = HostServices(env, sync_interval=args.sync_interval, gate=gate)
    for hub in hubs.values():
        services.attach_hub(hub)
    by_name = {name or "main": (name, cfg, scoped, base) for name, cfg, scoped, base in configs}

    async def build(key: str) -> TenantContext:
        name, cfg, scoped, base = by_name[key]
        return await build_tenant_context(
            cfg, scoped, base_path=base, instance_name=name,
            hub=hubs.get(cfg.database_url), allowed_harnesses=allowed_harnesses,
            host=services)

    services.cache.build = build

    @contextlib.asynccontextmanager
    async def lifespan(host):
        try:
            for key, (name, cfg, _, _) in by_name.items():
                hub = hubs.get(cfg.database_url)
                if hub is None:
                    # Its own store (SQLite, or a lone Postgres instance): only this
                    # process hears it, so its context stays built.
                    services.serve_local(await build(key))
                else:
                    services.tenants.serve(ServedTenant(key, cfg.db_schema or "public", hub=hub))
            for hub in hubs.values():
                # Env tenants get tenant_state rows too: one code path. Due at once,
                # so each is looked at once at boot (backfill, work a restart left).
                await hub.register_tenants([(t.name, t.key) for t in services.tenants.all()
                                            if t.hub is hub])
            if configs[0][0]:
                host.mount("/", EnvRouter(services.tenants))
            else:
                host.mount("/", services.cache.cached("main").app)
            services.start()
            yield
        finally:
            await services.stop()
            await services.cache.close()
            for hub in hubs.values():
                await hub.close()
            await asyncio.to_thread(shutdown_readers)

    host = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    host.state.run_gate = gate
    host.state.services = services
    return host


async def serve(args: argparse.Namespace) -> None:
    host = create_host(args)
    server = uvicorn.Server(uvicorn.Config(host, host=args.host, port=args.port,
                                           log_config=None, access_log=False, lifespan="on",
                                           timeout_graceful_shutdown=2))
    await server.serve()
    if not server.started:
        raise RuntimeError("handler host failed to start")


async def build_tenant_context(args: argparse.Namespace, env: Mapping[str, str], *,
                               host: "HostServices", base_path: str = "",
                               instance_name: str = "",
                               registry: IntegrationRegistry | None = None,
                               hub: PostgresHub | None = None,
                               plan_limits: LiveLimits | None = None,
                               allowed_harnesses: frozenset[str] | None = None) -> TenantContext:
    """Build one tenant's context (tenancy/contexts.py): its store, settings, gateway,
    executor, sinks, timer handlers and HTTP sub-app. Starts no long-lived task: runs,
    timer firings and deliveries are the host loops', MCP is the host's one app. The
    only tasks are integration start-up work that ends by itself (Crontal tool
    discovery, until 3b moves it into context loading); `close()` stops them.

    `allowed_harnesses` is the host's HANDLER_ALLOWED_HARNESSES (None: all).
    """
    tenant_token = set_current_tenant(instance_name or "main")
    try:
        return await _build_tenant_context(
            args, env, host=host, base_path=base_path, instance_name=instance_name,
            registry=registry, hub=hub, plan_limits=plan_limits,
            allowed_harnesses=allowed_harnesses)
    finally:
        reset_current_tenant(tenant_token)


class _TenantRuns:
    """A tenant's view of the host claim loop (what its dispatcher used to report)."""

    def __init__(self, runner: HostRunner, name: str) -> None:
        self._runner, self._name = runner, name

    @property
    def active_count(self) -> int:
        return self._runner.running(self._name)


async def _build_tenant_context(args, env, *, host, base_path, instance_name, registry, hub,
                                plan_limits, allowed_harnesses) -> TenantContext:
    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(env)
    if args.harness_env_file:
        env = {**read_env_file(args.harness_env_file), **env}
    integration_names = enabled_names(env)
    if integration_names is None:
        # Unconfigured: legacy wiring (crontal, email intake, google) as before.
        integrations = LoadedIntegrations(instance_name, [], configured=False)
    else:
        # Integrations see the same values the legacy wiring reads; CRONTAL_URL
        # comes from args so `--crontal-url` still wins.
        integration_env = {**env, "CRONTAL_URL": args.crontal_url or ""}
        integrations = (registry or default_registry).load(
            integration_names, instance_name=instance_name, env=integration_env,
            base_path=base_path, reserved_thread_types=CORE_THREAD_TYPES,
            reserved_timer_kinds=frozenset(kind.name for kind in CORE_TIMER_KINDS))
        logger.info("integrations enabled", extra={"instance": instance_name or "main",
                                                   "integrations": integrations.names})
        if "crontal" not in integrations.names and (args.crontal_url
                                                   or env.get("CRONTAL_AGENT_TOKEN")):
            # Setting the list replaces the legacy wiring: e.g. "mailbox" alone
            # turns Crontal off here. Use "crontal,mailbox" to keep it.
            logger.warning("CRONTAL_* is set but the crontal integration is not enabled; "
                           "this instance has no Crontal tools, webhooks or sink",
                           extra={"instance": instance_name or "main",
                                  "integrations": integrations.names})
    # Legacy crontal wiring runs only when HANDLER_INTEGRATIONS is unset; once it
    # is set, Crontal comes from the `crontal` integration (or not at all).
    legacy_crontal = not integrations.configured
    store = (PostgresStore(args.database_url, schema=args.db_schema, hub=hub,
                           max_size=int(env.get("HANDLER_DB_POOL_MAX", "5")))
             if args.database_url
             else SQLiteStore(data_dir / "handler.db"))
    if plan_limits is not None:
        store.limits = plan_limits
    store.on_due_changed = host.timer.wake  # its due-work writes wake the host timer
    name = instance_name or "main"
    target: TimerTarget | None = None
    workspace_sync: WorkspaceSync | None = None
    workspace_limit = None
    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    try:
        # Runs a dead worker left at 'running' (e.g. the dyno restarted mid-run).
        swept = await store.sweep_abandoned_runs()
        if swept:
            logger.warning("marked runs abandoned at startup",
                           extra={"instance": instance_name or "main", "runs": swept})
        # Threads an older release parked (threads.dead): open them again, with
        # their queued work set aside for the operator to replay, not run.
        for row in await store.sweep_dead_threads():
            log_run_failed(instance_name, row["thread_key"], row["thread_type"], row["run_id"],
                           row["attempts"], row["error"], row["event_ids"],
                           failed_before_upgrade=True)
        bus = host.bus.for_tenant(instance_name or "main")

        settings = SettingsService(
            store, InstanceDefaults(args.model, args.reasoning_effort),
            section_defaults=_settings_defaults(integrations, legacy_crontal and bool(
                args.crontal_url)))
        await settings.seed_from_files(
            Path(args.handler_yaml).expanduser() if args.handler_yaml else None,
            Path(args.agent_md).expanduser() if args.agent_md else None,
        )
        settings.allowed_harnesses = allowed_harnesses
        check_harness(args.default_harness, allowed_harnesses,
                      f"{instance_name or 'main'}: HANDLER_DEFAULT_HARNESS")
        if args.default_harness:
            # Fresh instances only: once threads settings exist, the UI owns them.
            threads_cfg, threads_version = await settings.get("threads")
            if threads_version is None:
                for per_type in threads_cfg.values():
                    per_type["harness"] = args.default_harness
                await settings.put("threads", threads_cfg, updated_by="default-harness",
                                   expected_version=None)
        if allowed_harnesses is not None:
            threads_cfg, _ = await settings.get("threads")
            refused = sorted(f"{kind}={entry['harness']}" for kind, entry in threads_cfg.items()
                             if entry.get("harness") not in allowed_harnesses)
            if refused:
                # Saved before the host allow-list: new threads of these types
                # would be refused at run time. Change them in Settings → Threads.
                logger.error("threads settings name a harness this host does not allow",
                             extra={"instance": instance_name or "main", "types": refused,
                                    "allowed": sorted(allowed_harnesses)})
        limits_cfg, _ = await settings.get("limits")
        grants_cfg, grants_version = await settings.get("grants")

        emit_event = thread_router(store)

        crontal_id = _crontal_identity_client(args, env, integrations, legacy_crontal)
        crontal_catalog = _crontal_route_catalog(args, env, integrations, legacy_crontal)
        users = [u.strip() for u in args.users.split(",") if u.strip()]
        # Output sinks: integrations register only when configured.
        outbox = Outbox(store, tenant=instance_name or "main")
        crontal_sink = crontal_sink_from_config(
            args.crontal_url, env.get("CRONTAL_AGENT_TOKEN"), env.get("HANDLER_WEBHOOK_SECRET"),
            instance_name) if legacy_crontal else None
        if crontal_sink is not None:
            outbox.register(crontal_sink)
            logger.info("crontal notification sink on", extra={"url": crontal_sink.url})
        notifier = Notifier(store, bus, outbox=outbox, users_provider=lambda: users,
                            instance=instance_name)
        memory_tools = MemoryTools(store, settings)
        scheduler = Scheduler(store, default_harness=settings.harness_source("automation"))
        scheduler.on_wake = host.timer.wake
        if plan_limits is not None:
            scheduler.paused = lambda: not plan_limits.current.automations
            async def tell_skipped(schedule, over) -> None:
                recipients = schedule.notify or ([schedule.created_by] if schedule.created_by else [])
                await notifier.send(
                    f"automation:{schedule.id}", None,
                    f"Automation \"{schedule.name}\" was skipped: this workspace has used all "
                    f"{over.limit} inputs in its plan this month. It runs again after "
                    f"{over.resets_at.date().isoformat()}.", recipients)
            scheduler.on_skipped = tell_skipped
        gateway = Gateway(store, GrantPolicy(grants_cfg, plan_limits=plan_limits),
                          emit_event=emit_event)
        questions = QuestionService(store, emit_event=emit_event, notifier=notifier)
        from .tools.approvals import ApprovalContinuationTools
        from .tools.task_outcomes import TaskOutcomeTools
        for provider in (memory_tools, ThreadTools(store, bus), ApprovalContinuationTools(store),
                         TaskOutcomeTools(store), NotifyTools(store, bus, notifier=notifier),
                         QuestionTools(questions)):
            gateway.register(provider)
        # Registered on every plan: each run's tool view and every call follow the
        # live plan (GrantPolicy.excluded), so an upgrade or downgrade needs no restart.
        gateway.register(AutomationTools(store, scheduler))
        tokens = host.tokens  # one MCP app for the host; a token names its gateway

        config = RunConfig(data_dir=data_dir, reasoning_effort=args.reasoning_effort,
                           timeout_seconds=float(limits_cfg["run_timeout_seconds"]),
                           max_attempts=int(limits_cfg["max_attempts"]),
                           instance_name=instance_name or "main",
                           default_model=args.model, plan_limits=plan_limits,
                           allowed_harnesses=allowed_harnesses)
        local_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        file_store = (S3FileStore.from_env(prefix=env.get("HANDLER_FILE_PREFIX", ""), env=dict(env))
                      if args.file_store == "s3" else LocalFileStore(data_dir / "files"))
        from .artifacts import ArtifactStore
        from .tools.spreadsheets import SpreadsheetTools
        artifact_store = ArtifactStore(file_store)
        gateway.register(SpreadsheetTools(
            lambda key: config.workspaces_dir / thread_slug(key), artifact_store))
        harnesses: dict = {}
        codex_allowed = allowed_harnesses is None or HarnessName.CODEX.value in allowed_harnesses
        if plan_limits is not None and not plan_limits.current.allow_codex:
            settings.only_harness = HarnessName.OPENAI_AGENTS.value
        elif not codex_allowed:
            logger.info("codex harness off: not in %s", ALLOWED_HARNESSES_ENV)
        else:
            try:
                harnesses["codex"] = CodexHarness(
                    env.get("HANDLER_CODEX_BIN") or codex_binary(),
                    default_model=args.model,
                    default_reasoning_effort=args.reasoning_effort,
                    auth_json=Path(args.codex_auth_json).expanduser() if args.codex_auth_json else None,
                    sandbox=args.codex_sandbox,
                    auth_json_link=args.codex_auth_link,
                    environment=env,
                )
            except FileNotFoundError as exc:
                logger.warning("codex harness off: %s", exc)
        api_key = env.get("OPENAI_API_KEY") or env.get("CODEX_API_KEY")
        if allowed_harnesses is not None and HarnessName.OPENAI_AGENTS.value not in allowed_harnesses:
            logger.info("openai_agents harness off: not in %s", ALLOWED_HARNESSES_ENV)
        elif api_key:
            harnesses["openai_agents"] = OpenAIAgentsHarness(
                api_key, default_model=args.model, default_reasoning_effort=args.reasoning_effort)
        else:
            logger.info("openai_agents harness off: set OPENAI_API_KEY to enable it")
        if not harnesses:
            raise SystemExit("no harness available: install Codex or set OPENAI_API_KEY")

        async def harness_defaults(name: str) -> dict:
            harness = harnesses.get(name)
            return await harness.defaults() if hasattr(harness, "defaults") else {}

        settings.harness_defaults = harness_defaults
        workspace_sync = WorkspaceSync(file_store, data_dir)
        executor = RunExecutor(
            store, harnesses, bus, config, PriceTable.from_yaml(),
            tokens=tokens, mcp_url=f"http://{local_host}:{args.port}{base_path}/mcp/",
            settings=settings, memory_tools=memory_tools,
            workspace_sync=workspace_sync,
            crontal_identity=crontal_id, crontal_catalog=crontal_catalog,
        )
        executor.gateway = gateway
        lane_capacity = {Lane.INTERACTIVE: int(limits_cfg["interactive_capacity"]),
                         Lane.BACKGROUND: int(limits_cfg["background_capacity"])}

        trusted_user = env.get("HANDLER_TRUSTED_USER", "").strip() or None
        trusted_devices = _trusted_device_users(env.get("HANDLER_TRUSTED_DEVICE_USERS", ""))
        auth = Authenticator(store, args.admin_key, trusted_user_id=trusted_user,
                             trusted_device_users=trusted_devices)
        if trusted_user or trusted_devices:
            logger.warning("trusted network authentication on", extra={
                "user": trusted_user, "device_count": len(trusted_devices)})
        if not args.admin_key:
            logger.warning("no admin key set; only user tokens can authenticate")
        uploads = UploadStore(data_dir / "uploads", limit=(
            (lambda: plan_limits.current.max_upload_bytes) if plan_limits is not None else None))
        if plan_limits is not None:
            # Counted once here, off the event loop; writes then keep the totals current.
            workspace_limit = register_workspace_limit(
                config.workspaces_dir, lambda: plan_limits.current.max_workspace_bytes)
            await workspace_limit.prime()
            await uploads.used.prime()
        app = create_app(store, bus, auth, default_harness=settings.harness_source("chat"),
                         uploads=uploads, instance_name=instance_name,
                         cancel_wake=executor.wake_cancel)
        app.mount("/mcp", host.mcp)
        app.include_router(approvals_router(gateway, store, auth))
        app.include_router(memory_recovery_router(gateway, store, auth))
        app.include_router(questions_router(questions, store, auth))
        app.include_router(tasks_router(store, bus, auth))
        app.include_router(usage_router(store, auth))
        app.include_router(automation_dashboard_router(store, auth, settings))
        store.on_routed = lambda event, spec: publish_queued(bus, event, spec)
        async def effective_tool_names(ctx):
            return {spec.name for spec in await gateway.specs_for(ctx)}

        app.include_router(settings_router(settings, auth, memory_tools,
                                           crontal_identity=crontal_id,
                                           crontal_catalog=crontal_catalog,
                                           available_tools=lambda: {s.name for s in gateway.specs()},
                                           available_tools_for=effective_tool_names))
        app.include_router(admin_router(store, auth, scheduler=scheduler, gateway=gateway,
                                        uploads=uploads, log_file=_log_file(args),
                                        artifact_store=artifact_store, outbox=outbox))
        email_secret = env.get("HANDLER_EMAIL_SECRET")
        intake_observers: list = []  # integrations add theirs below
        if email_secret:
            app.include_router(email_ingest_router(store, email_secret, settings, workspace_sync,
                                                   harness=settings.harness_source("email"),
                                                   crontal_identity=crontal_id,
                                                   observers=intake_observers))
        else:
            logger.info("email intake off: set HANDLER_EMAIL_SECRET to enable /ingest/email")
        google_oauth = google_oauth_from_env(store, env=env)  # None unless GOOGLE_OAUTH_* are set
        app.include_router(google_router(google_oauth, auth, plan_limits=plan_limits,
                                         return_to_origins=env.get("HANDLER_RETURN_TO_ORIGINS", "")))
        if google_oauth is not None:
            gateway.register(GmailTools(
                google_oauth,
                workspace_for=lambda key: config.workspaces_dir / thread_slug(key),
                owner_for=lambda ctx: (user_owner(ctx.user_id)
                                       if ctx.user_id else INSTANCE_OWNER)))
        webhook_secret = env.get("HANDLER_WEBHOOK_SECRET")
        if webhook_secret and legacy_crontal:
            app.include_router(crontal_ingest_router(store, webhook_secret,
                                                     harness=settings.harness_source("crontal")))
        app.include_router(ui_manifest_router(integrations, auth))
        if plan_limits is not None:
            app.add_middleware(ConsoleGuard, limits=plan_limits)
        if plan_limits is not None and not plan_limits.current.connected_mail:
            @app.post("/api/mailboxes")
            async def mailboxes_not_in_plan(_principal: Principal = Depends(auth.require_admin)):
                return JSONResponse(upgrade_required("mailbox", plan_limits.current), status_code=403)

        # Grants: a save in this process applies at once (settings listener); one
        # made elsewhere is picked up by the version check before each run.
        grants_seen = {"version": grants_version}

        def take_grants(value: dict, version: int | None) -> None:
            if version == grants_seen["version"]:
                return
            gateway.set_policy(GrantPolicy(value, plan_limits=plan_limits))
            grants_seen["version"] = version
            logger.info("grants reloaded", extra={"version": version})

        def grants_saved(section: str, stored) -> None:
            if section == "grants":
                take_grants(stored.value, stored.version)

        async def refresh_grants() -> None:
            take_grants(*await settings.get("grants"))

        settings.on_saved.append(grants_saved)
        executor.before_run = refresh_grants

        crontal_token = env.get("CRONTAL_AGENT_TOKEN")
        if legacy_crontal and args.crontal_url and crontal_token:
            gateway.register(CrontalApiTools(
                crontal_base_url(args.crontal_url), crontal_token,
                workspace_for=lambda key: config.workspaces_dir / thread_slug(key)))

        async def start_crontal_connector() -> None:
            """Discover Crontal's tools (retrying until reachable), then expose them."""
            if legacy_crontal:
                await run_crontal_connector(args.crontal_url, env.get("CRONTAL_AGENT_TOKEN"),
                                            gateway, stop)

        # Integration contributions, after all core wiring so duplicates of core
        # routes and tools are reported with the integration's name.
        integration_ctx = IntegrationContext(
            instance_name=instance_name, base_path=base_path, env=env, store=store,
            bus=bus, settings=settings, gateway=gateway, outbox=outbox,
            notifier=notifier, auth=auth, workspaces_dir=config.workspaces_dir,
            workspace_sync=workspace_sync, file_store=file_store)
        contributions = integrations.collect_runtime(
            integration_ctx,
            reserved_routes=set(route_keys(app)),
            reserved_tools={spec.name for spec in gateway.specs()})
        for _, tool_set in contributions.tool_sets:
            gateway.register(tool_set)
        config.available_tools = frozenset(s.name for s in gateway.specs())
        config.available_tools_for = effective_tool_names
        for _, sink in contributions.sinks:
            outbox.register(sink)
        intake_observers.extend(o for _, o in contributions.email_intake_observers)
        for _, router in contributions.routers:
            app.include_router(router)
        app.add_middleware(TenantScope, tenant=instance_name or "main")
        app.state.integrations = integrations
        app.state.gateway, app.state.outbox = gateway, outbox
        lifecycle = SimpleNamespace(dispatcher=_TenantRuns(host.runner, name), store=store,
                                    outbox=outbox,
                                    workspace_sync=workspace_sync, limits=plan_limits,
                                    tokens=tokens, bus=bus, uploads=uploads,
                                    workspaces_dir=config.workspaces_dir,
                                    scheduler=scheduler, gateway=gateway, questions=questions,
                                    executor=executor)
        app.state.lifecycle = lifecycle

        # Time-driven work: the host timer calls these for this tenant's due rows.
        target = TimerTarget(instance_name, store.due_tenant, store.due_queue())
        for kind in CORE_TIMER_KINDS:
            if kind.name == INPUTS_RELEASE and not hasattr(store, "release_held_due"):
                continue  # held inputs exist on registry (Postgres) tenants only
            target.bind(kind, lifecycle)
        for _, kind in integrations.timer_kinds.values():
            target.bind(kind, integration_ctx)

        async def backfill_due() -> None:
            """Make this tenant's due rows match its data (at start, and when its plan
            changes). Idempotent; a missed schedule fires once, not per interval."""
            if plan_limits is not None:
                # What a new month or plan allows runs now; the rest waits for the
                # `inputs.release` row at the month boundary.
                await store.release_held_inputs()
            await store.backfill_due_work(schedules=not scheduler._is_paused(),
                                          held=plan_limits is not None)
            for owner, kind in integrations.timer_kinds.values():
                if kind.backfill is not None:
                    try:
                        await kind.backfill(integration_ctx)
                    except Exception:
                        logger.exception("timer backfill failed",
                                         extra={"integration": owner, "kind": kind.name})

        lifecycle.backfill_due = backfill_due
        app.state.timer_target = target
        await backfill_due()
        host.workspaces.register(name, workspace_sync)
        host.wire(name, store, outbox)
        app.state.identity_provider = (contributions.identity_provider[1]
                                       if contributions.identity_provider else None)

        # Integration start-up work that ends by itself (tool discovery with retry).
        tasks.append(asyncio.create_task(start_crontal_connector(), name="crontal-connector"))
        tasks.extend(asyncio.create_task(task.run(stop), name=f"{owner}:{task.name}")
                     for owner, task in contributions.background_tasks)
        await asyncio.sleep(0)  # let them start before the context is used
    except BaseException:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if workspace_sync is not None:
            host.workspaces.unregister(workspace_sync)
        unregister_workspace_limit(data_dir / "workspaces", workspace_limit)
        await store.close()
        raise

    async def close() -> None:
        """Evicted, invalidated or shut down: nothing of this tenant stays running."""
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _flush_until(workspace_sync, SHUTDOWN_BUDGET_SECONDS)
        if not workspace_sync.dirty:  # else the host backstop keeps retrying it
            host.workspaces.unregister(workspace_sync)
        unregister_workspace_limit(data_dir / "workspaces", workspace_limit)
        await store.close()

    logger.info("tenant context ready", extra={"tenant": name, "data_dir": str(data_dir),
                                               "tools": len(gateway.specs())})
    return TenantContext(
        name=name, key=store.due_tenant, store=store, app=app, executor=executor,
        gateway=gateway, outbox=outbox, settings=settings, limits=plan_limits,
        timer_target=target, workspace_sync=workspace_sync, lifecycle=lifecycle,
        lane_capacity=lane_capacity, max_concurrent=args.max_concurrent,
        lease_seconds=config.lease_seconds, close=close)


@contextlib.asynccontextmanager
async def instance_runtime(args: argparse.Namespace, env: Mapping[str, str], *,
                           base_path: str = "", instance_name: str = "",
                           registry: IntegrationRegistry | None = None,
                           hub: PostgresHub | None = None, gate: RunGate | None = None,
                           plan_limits: LiveLimits | None = None,
                           allowed_harnesses: frozenset[str] | None = None,
                           host: HostServices | None = None):
    """One instance's app, for as long as the block runs.

    Without `host` this is a lone instance: it makes its own host services and runs
    them (the same host loops, one tenant). With one, the context is pinned in that
    host (a tenant on its own store, which only this process hears).
    """
    own_host = host is None
    if host is None:
        host = HostServices(env, sync_interval=args.sync_interval, gate=gate)
        if hub is not None:
            host.attach_hub(hub)
    context = await build_tenant_context(
        args, env, host=host, base_path=base_path, instance_name=instance_name,
        registry=registry, hub=hub, plan_limits=plan_limits,
        allowed_harnesses=allowed_harnesses)
    if hub is not None:
        host.tenants.serve(ServedTenant(context.name, context.key, hub=hub))
        host.cache.pin(context)
        await hub.register_tenants([(context.name, context.key)])
    else:
        host.serve_local(context)
    try:
        if own_host:
            host.start()
        yield context.app
    finally:
        if own_host:
            await host.stop()
        host.tenants.drop(context.name)
        await context.close()


SHUTDOWN_BUDGET_SECONDS = 25.0


async def _flush_until(sync: WorkspaceSync, seconds: float) -> None:
    if not sync.dirty:
        return
    try:
        synced = await asyncio.wait_for(sync.flush(), timeout=max(seconds, 1))
        logger.info("shutdown: workspaces synced", extra={"threads": synced})
    except TimeoutError:
        logger.warning("shutdown: workspace sync cut short",
                       extra={"unsynced": sorted(sync.dirty)})
    except Exception:
        logger.exception("shutdown: workspace sync failed",
                         extra={"unsynced": sorted(sync.dirty)})


async def _import_legacy(args: argparse.Namespace) -> None:
    from .migrate_legacy import import_legacy

    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(data_dir / "handler.db")
    try:
        report = await import_legacy(Path(args.source), store, SettingsService(store),
                                     default_user=args.default_user)
    finally:
        await store.close()
    print("\n".join(report.lines()))


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.command == "serve":
        configure_json_logging(args.log_level, file=_log_file(args))
        asyncio.run(serve(args))
    elif args.command == "import-legacy":
        asyncio.run(_import_legacy(args))
    elif args.command == "google" and args.google_command == "configure":
        _configure_google(args)
    else:
        raise SystemExit(run_cli(args))


if __name__ == "__main__":
    main()
