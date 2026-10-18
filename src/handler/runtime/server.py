"""`handler-rt serve`: the runtime in one process.

Web API + MCP gateway + dispatcher + scheduler, sharing one store.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import ipaddress
import logging
import os
import re
import secrets
import shlex
from collections.abc import Mapping

from fastapi import FastAPI, HTTPException
from pathlib import Path

import uvicorn

from .auth import Authenticator, add_cli_commands, run_cli
from .bus import RunEventBus
from .dispatcher import Dispatcher
from .files import LocalFileStore, S3FileStore
from .gateway.api import approvals_router
from .gateway.crontal import crontal_connector
from .gateway.gateway import Gateway
from .gateway.gmail import GmailTools
from .gateway.google_oauth import INSTANCE_OWNER, google_oauth_from_env, user_owner
from .gateway.mcp_server import create_mcp_app
from .gateway.policy import GrantPolicy
from .gateway.tokens import RunTokens
from .harness.codex import SANDBOX_MODES, CodexHarness, codex_binary
from .gateway.crontal_api import CrontalApiTools, crontal_base_url
from .harness.openai_agents import OpenAIAgentsHarness
from .integrations.crontal.sink import crontal_sink_from_config
from .integrations.base import IntegrationContext
from .integrations.registry import (
    IntegrationRegistry,
    LoadedIntegrations,
    default_registry,
    enabled_names,
    route_keys,
)
from .memory import MemoryTools
from .notifications import FailureNotifier, Notifier
from .observability.logging import configure_json_logging
from .observability.pricing import PriceTable
from .run import RunConfig, RunExecutor, thread_slug
from .scheduler import Scheduler
from .settings import HarnessName, InstanceDefaults, SettingsService
from .sinks import Outbox
from .store.postgres import PostgresStore
from .store.sqlite import SQLiteStore
from .tasks import publish_queued
from .tools.automation import AutomationTools
from .tools.notify import NotifyTools
from .tools.threads import ThreadTools
from .types import Event, Lane, ThreadSpec
from .uploads import UploadStore
from .web.admin_api import admin_router
from .web.app import create_app
from .web.email_ingest import email_ingest_router
from .web.google_api import google_router
from .web.ingest import crontal_ingest_router
from .web.settings_api import settings_router
from .web.tasks_api import tasks_router
from .web.ui_manifest_api import ui_manifest_router
from .workspaces import WorkspaceSync

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

# Thread types the core owns; integrations may not redefine them.
CORE_THREAD_TYPES = frozenset({"chat", "automation"})


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
    return configs


def create_host(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> FastAPI:
    configs = instance_configs(args, dict(os.environ if env is None else env))

    @contextlib.asynccontextmanager
    async def lifespan(host):
        stop = asyncio.Event()
        tasks = []

        async def own_runtime(name, cfg, scoped, base, ready):
            # MCP uses AnyIO cancel scopes: enter AND exit each instance in the
            # same task. Separate owner tasks let them drain concurrently.
            try:
                async with instance_runtime(cfg, scoped, base_path=base,
                                            instance_name=name) as app:
                    ready.set_result(app)
                    await stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                raise

        try:
            for name, cfg, scoped, base in configs:
                ready = asyncio.get_running_loop().create_future()
                tasks.append(asyncio.create_task(own_runtime(name, cfg, scoped, base, ready),
                                                  name=f"instance-{name or 'default'}"))
                app = await ready
                host.mount(base or "/", app)
            if configs[0][0]:
                async def unknown_instance(path: str = ""):
                    raise HTTPException(404, "instance not found")
                host.add_api_route("/i/{path:path}", unknown_instance,
                                   methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
                main = next(route.app for route in host.routes if getattr(route, "path", "") == "/i/main")
                host.mount("/", main)
            yield
        finally:
            stop.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.error("instance lifecycle failed", exc_info=result)

    return FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


async def serve(args: argparse.Namespace) -> None:
    host = create_host(args)
    server = uvicorn.Server(uvicorn.Config(host, host=args.host, port=args.port,
                                           log_config=None, access_log=False, lifespan="on",
                                           timeout_graceful_shutdown=2))
    await server.serve()
    if not server.started:
        raise RuntimeError("handler host failed to start")


@contextlib.asynccontextmanager
async def instance_runtime(args: argparse.Namespace, env: Mapping[str, str], *,
                           base_path: str = "", instance_name: str = "",
                           registry: IntegrationRegistry | None = None):
    """Own one instance and close all its resources when the host stops."""
    integration_names = enabled_names(env)
    if integration_names is None:
        # Unconfigured: legacy wiring only (phase 1 keeps it in every case).
        integrations = LoadedIntegrations(instance_name, [], configured=False)
    else:
        integrations = (registry or default_registry).load(
            integration_names, instance_name=instance_name, env=env, base_path=base_path,
            reserved_thread_types=CORE_THREAD_TYPES)
        logger.info("integrations enabled", extra={"instance": instance_name or "main",
                                                   "integrations": integrations.names})
    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(env)
    if args.harness_env_file:
        env = {**read_env_file(args.harness_env_file), **env}
    store = (PostgresStore(args.database_url, schema=args.db_schema,
                           max_size=int(env.get("HANDLER_DB_POOL_MAX", "5")))
             if args.database_url
             else SQLiteStore(data_dir / "handler.db"))
    try:
        bus = RunEventBus()

        settings = SettingsService(
            store, InstanceDefaults(args.model, args.reasoning_effort))
        await settings.seed_from_files(
            Path(args.handler_yaml).expanduser() if args.handler_yaml else None,
            Path(args.agent_md).expanduser() if args.agent_md else None,
        )
        if args.default_harness:
            # Fresh instances only: once threads settings exist, the UI owns them.
            threads_cfg, threads_version = await settings.get("threads")
            if threads_version is None:
                for per_type in threads_cfg.values():
                    per_type["harness"] = args.default_harness
                await settings.put("threads", threads_cfg, updated_by="default-harness",
                                   expected_version=None)
        limits_cfg, _ = await settings.get("limits")
        grants_cfg, grants_version = await settings.get("grants")

        emit_event = thread_router(store)

        users = [u.strip() for u in args.users.split(",") if u.strip()]
        # Output sinks: integrations register only when configured.
        outbox = Outbox(store)
        crontal_sink = crontal_sink_from_config(
            args.crontal_url, env.get("CRONTAL_AGENT_TOKEN"), env.get("HANDLER_WEBHOOK_SECRET"),
            instance_name)
        if crontal_sink is not None:
            outbox.register(crontal_sink)
            logger.info("crontal notification sink on", extra={"url": crontal_sink.url})
        notifier = Notifier(store, bus, outbox=outbox, users_provider=lambda: users,
                            instance=instance_name)
        memory_tools = MemoryTools(store)
        scheduler = Scheduler(store, default_harness=settings.harness_source("automation"))
        gateway = Gateway(store, GrantPolicy(grants_cfg), emit_event=emit_event)
        for provider in (memory_tools, AutomationTools(store, scheduler), ThreadTools(store, bus),
                         NotifyTools(store, bus, notifier=notifier)):
            gateway.register(provider)
        tokens = RunTokens()
        mcp_app = create_mcp_app(gateway, tokens)

        config = RunConfig(data_dir=data_dir, reasoning_effort=args.reasoning_effort,
                           timeout_seconds=float(limits_cfg["run_timeout_seconds"]),
                           max_attempts=int(limits_cfg["max_attempts"]))
        local_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        file_store = (S3FileStore.from_env(prefix=env.get("HANDLER_FILE_PREFIX", ""), env=dict(env))
                      if args.file_store == "s3" else LocalFileStore(data_dir / "files"))
        harnesses: dict = {}
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
        if api_key:
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
            workspace_sync=workspace_sync, failure_notifier=FailureNotifier(notifier),
        )
        dispatcher = Dispatcher(
            store, executor, lease_seconds=config.lease_seconds,
            max_concurrent=args.max_concurrent,
            lane_capacity={Lane.INTERACTIVE: int(limits_cfg["interactive_capacity"]),
                           Lane.BACKGROUND: int(limits_cfg["background_capacity"])},
        )

        trusted_user = env.get("HANDLER_TRUSTED_USER", "").strip() or None
        trusted_devices = _trusted_device_users(env.get("HANDLER_TRUSTED_DEVICE_USERS", ""))
        auth = Authenticator(store, args.admin_key, trusted_user_id=trusted_user,
                             trusted_device_users=trusted_devices)
        if trusted_user or trusted_devices:
            logger.warning("trusted network authentication on", extra={
                "user": trusted_user, "device_count": len(trusted_devices)})
        if not args.admin_key:
            logger.warning("no admin key set; only user tokens can authenticate")
        uploads = UploadStore(data_dir / "uploads")
        app = create_app(store, bus, auth, default_harness=settings.harness_source("chat"),
                         uploads=uploads, instance_name=instance_name)
        app.mount("/mcp", mcp_app)
        app.include_router(approvals_router(gateway, store, auth))
        app.include_router(tasks_router(store, bus, auth))
        store.on_routed = lambda event, spec: publish_queued(bus, event, spec)
        app.include_router(settings_router(settings, auth, memory_tools))
        app.include_router(admin_router(store, auth, scheduler=scheduler, gateway=gateway,
                                        uploads=uploads, log_file=_log_file(args)))
        email_secret = env.get("HANDLER_EMAIL_SECRET")
        if email_secret:
            app.include_router(email_ingest_router(store, email_secret, settings, workspace_sync,
                                                   harness=settings.harness_source("email")))
        else:
            logger.info("email intake off: set HANDLER_EMAIL_SECRET to enable /ingest/email")
        google_oauth = google_oauth_from_env(store, env=env)  # None unless GOOGLE_OAUTH_* are set
        app.include_router(google_router(google_oauth, auth,
                                         return_to_origins=env.get("HANDLER_RETURN_TO_ORIGINS", "")))
        if google_oauth is not None:
            gateway.register(GmailTools(
                google_oauth,
                workspace_for=lambda key: config.workspaces_dir / thread_slug(key),
                owner_for=lambda ctx: (user_owner(ctx.user_id)
                                       if ctx.user_id else INSTANCE_OWNER)))
        webhook_secret = env.get("HANDLER_WEBHOOK_SECRET")
        if webhook_secret:
            app.include_router(crontal_ingest_router(store, webhook_secret,
                                                     harness=settings.harness_source("crontal")))
        app.include_router(ui_manifest_router(integrations, auth))

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def housekeeping() -> None:
            """Expire stale approvals and pick up grant changes from settings."""
            version = grants_version
            while not stop.is_set():
                try:
                    await gateway.expire_stale()
                    grants, latest = await settings.get("grants")
                    if latest != version:
                        gateway.set_policy(GrantPolicy(grants))
                        version = latest
                        logger.info("grants reloaded", extra={"version": latest})
                except Exception:
                    logger.exception("housekeeping failed")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=15)

        crontal_token = env.get("CRONTAL_AGENT_TOKEN")
        if args.crontal_url and crontal_token:
            gateway.register(CrontalApiTools(
                crontal_base_url(args.crontal_url), crontal_token,
                workspace_for=lambda key: config.workspaces_dir / thread_slug(key)))

        async def start_crontal_connector() -> None:
            """Discover Crontal's tools (retrying until reachable), then expose them."""
            token = env.get("CRONTAL_AGENT_TOKEN")
            if not args.crontal_url or not token:
                if args.crontal_url:
                    logger.error("CRONTAL_URL set but CRONTAL_AGENT_TOKEN missing; Crontal tools off")
                return
            connector = crontal_connector(args.crontal_url, token)
            delay = 5.0
            while not stop.is_set():
                try:
                    await connector.start()
                    gateway.register(connector)
                    logger.info("crontal tools registered",
                                extra={"tools": [s.name for s in connector.tools()]})
                    return
                except Exception as exc:
                    logger.warning("crontal connector not ready, retrying",
                                   extra={"error": str(exc), "retry_in": delay})
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=delay)
                    delay = min(delay * 2, 300.0)

        # Integration contributions, after all core wiring so duplicates of core
        # routes and tools are reported with the integration's name.
        contributions = integrations.collect_runtime(
            IntegrationContext(
                instance_name=instance_name, base_path=base_path, env=env, store=store,
                bus=bus, settings=settings, gateway=gateway, outbox=outbox,
                notifier=notifier, auth=auth, workspaces_dir=config.workspaces_dir,
                workspace_sync=workspace_sync, file_store=file_store),
            reserved_routes=set(route_keys(app)),
            reserved_tools={spec.name for spec in gateway.specs()})
        for _, tool_set in contributions.tool_sets:
            gateway.register(tool_set)
        for _, sink in contributions.sinks:
            outbox.register(sink)
        for _, router in contributions.routers:
            app.include_router(router)
        app.state.integrations = integrations
        app.state.identity_provider = (contributions.identity_provider[1]
                                       if contributions.identity_provider else None)

        async with mcp_app.lifespan():
            tasks = [
                asyncio.create_task(start_crontal_connector(), name="crontal-connector"),
                asyncio.create_task(dispatcher.run_forever(stop), name="dispatcher"),
                asyncio.create_task(scheduler.run_forever(stop), name="scheduler"),
                asyncio.create_task(housekeeping(), name="housekeeping"),
                asyncio.create_task(outbox.run_forever(stop), name="outbox"),
                asyncio.create_task(workspace_sync.run_periodic(stop, args.sync_interval),
                                    name="workspace-sync"),
            ]
            tasks.extend(asyncio.create_task(task.run(stop), name=f"{owner}:{task.name}")
                         for owner, task in contributions.background_tasks)
            logger.info("handler-rt serving", extra={"url": f"http://{args.host}:{args.port}/login",
                                                      "data_dir": str(data_dir),
                                                      "tools": [s.name for s in gateway.specs()]})
            try:
                yield app
            finally:
                stop.set()
                deadline = loop.time() + SHUTDOWN_BUDGET_SECONDS
                await _flush_until(workspace_sync, deadline - loop.time())
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                                           timeout=max(deadline - loop.time() - 5, 1))
                except TimeoutError:
                    logger.warning("shutdown: tasks still running at the deadline")
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await _flush_until(workspace_sync, deadline - loop.time())

    finally:
        await store.close()


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
