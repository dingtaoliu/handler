"""`handler-rt serve`: the runtime in one process.

Web API + MCP gateway + dispatcher + scheduler, sharing one store.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

import uvicorn

from .auth import Authenticator, add_cli_commands, run_cli
from .bus import RunEventBus
from .dispatcher import Dispatcher
from .files import LocalFileStore, S3FileStore
from .gateway.api import approvals_router
from .gateway.crontal import crontal_connector
from .gateway.gateway import Gateway
from .gateway.mcp_server import create_mcp_app
from .gateway.policy import GrantPolicy
from .gateway.tokens import RunTokens
from .harness.codex import SANDBOX_MODES, CodexHarness, codex_binary
from .gateway.crontal_api import CrontalApiTools, crontal_base_url
from .harness.openai_agents import OpenAIAgentsHarness
from .memory import MemoryTools
from .observability.logging import configure_json_logging
from .observability.pricing import PriceTable
from .run import RunConfig, RunExecutor, thread_slug
from .scheduler import Scheduler
from .settings import HarnessName, InstanceDefaults, SettingsService
from .store.postgres import PostgresStore
from .store.sqlite import SQLiteStore
from .tools.automation import AutomationTools
from .tools.notify import NotifyTools
from .types import Event, Lane, ThreadSpec
from .uploads import UploadStore
from .web.admin_api import admin_router
from .web.app import create_app
from .web.ingest import crontal_ingest_router
from .web.settings_api import settings_router
from .workspaces import WorkspaceSync

logger = logging.getLogger("handler.runtime.server")


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


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="handler-rt", description="Handler runtime (rebuild)")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve", help="run the web API, MCP gateway, dispatcher and scheduler")
    s.add_argument("--data-dir", default=os.environ.get("HANDLER_RT_DATA_DIR", "~/.handler-rt"))
    s.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                   help="Postgres URL; when set, use PostgresStore instead of SQLite")
    s.add_argument("--db-schema", default=os.environ.get("HANDLER_DB_SCHEMA"),
                   help="Postgres schema for all tables (created if missing)")
    s.add_argument("--harness-env-file", default=os.environ.get("HANDLER_HARNESS_ENV_FILE"),
                   help="KEY=VALUE file loaded into the environment the harness inherits")
    s.add_argument("--host", default=os.environ.get("HANDLER_RT_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8100)))
    s.add_argument("--file-store", choices=("local", "s3"),
                   default=os.environ.get("HANDLER_FILE_STORE", "local"),
                   help="workspace backups: local data-dir/files or Bucketeer S3")
    s.add_argument("--model", default=os.environ.get("HANDLER_RT_MODEL"),
                   help="default model when the thread-type settings don't set one")
    s.add_argument("--reasoning-effort", default=os.environ.get("HANDLER_RT_EFFORT", "low"))
    s.add_argument("--codex-auth-json", default=os.environ.get("HANDLER_RT_CODEX_AUTH"),
                   help="auth.json copied into each isolated CODEX_HOME (dev only)")
    s.add_argument("--handler-yaml", default=None, help="seed settings from this file once")
    s.add_argument("--agent-md", default=None, help="seed persona from this AGENT.md once")
    s.add_argument("--users", default=os.environ.get("HANDLER_RT_USERS", ""),
                   help="comma-separated user ids (for notify to 'all')")
    s.add_argument("--max-concurrent", type=int,
                   default=int(os.environ["HANDLER_MAX_CONCURRENT"])
                   if os.environ.get("HANDLER_MAX_CONCURRENT") else None,
                   help="maximum active runs across all lanes")
    s.add_argument("--codex-auth-link", action="store_true",
                   default=os.environ.get("HANDLER_RT_CODEX_AUTH_LINK", "") == "1",
                   help="symlink --codex-auth-json into each harness home instead of "
                        "copying it (keeps one shared ChatGPT login fresh)")
    s.add_argument("--codex-sandbox",
                   default=os.environ.get("HANDLER_CODEX_SANDBOX", "workspace-write"),
                   choices=SANDBOX_MODES,
                   help="Codex sandbox mode; use danger-full-access only where the container "
                        "itself is the isolation boundary (e.g. Heroku dynos)")
    s.add_argument("--sync-interval", type=float,
                   default=float(os.environ.get("HANDLER_SYNC_INTERVAL_SECONDS", "300")),
                   help="seconds between background workspace uploads (S3 file store)")
    s.add_argument("--default-harness", default=os.environ.get("HANDLER_DEFAULT_HARNESS"),
                   choices=[h.value for h in HarnessName],
                   help="harness for every thread type on a fresh instance "
                        "(later changes go through settings)")
    s.add_argument("--crontal-url", default=os.environ.get("CRONTAL_URL"),
                   help="Crontal MCP endpoint, e.g. https://crontal.ai/api/v1/agent-mcp "
                        "(token from $CRONTAL_AGENT_TOKEN)")
    s.add_argument("--admin-key", default=os.environ.get("HANDLER_ADMIN_KEY"),
                   help="instance admin key (bearer credential with admin rights)")
    s.add_argument("--log-level", default="INFO")
    s.add_argument("--log-file", default=os.environ.get("HANDLER_RT_LOG_FILE"),
                   help="rotating JSON-lines log file (default <data-dir>/logs/handler-rt.jsonl)")
    add_cli_commands(sub)
    imp = sub.add_parser("import-legacy",
                         help="copy persona, memory, users and chats from a legacy handler")
    imp.add_argument("--from", dest="source", required=True,
                     help="legacy instance dir, e.g. ~/.handler/instances/personal")
    imp.add_argument("--data-dir", default=os.environ.get("HANDLER_RT_DATA_DIR", "~/.handler-rt"))
    imp.add_argument("--default-user", default="danny",
                     help="owner for legacy data without a user id")
    return p.parse_args(argv)


async def serve(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    if args.harness_env_file:
        load_env_file(args.harness_env_file)
    store = (PostgresStore(args.database_url, schema=args.db_schema)
             if args.database_url
             else SQLiteStore(data_dir / "handler.db"))
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
    memory_tools = MemoryTools(store)
    scheduler = Scheduler(store, default_harness=settings.harness_source("automation"))
    gateway = Gateway(store, GrantPolicy(grants_cfg), emit_event=emit_event)
    for provider in (memory_tools, AutomationTools(store, scheduler),
                     NotifyTools(store, bus, users_provider=lambda: users)):
        gateway.register(provider)
    tokens = RunTokens()
    mcp_app = create_mcp_app(gateway, tokens)

    config = RunConfig(data_dir=data_dir, reasoning_effort=args.reasoning_effort,
                       timeout_seconds=float(limits_cfg["run_timeout_seconds"]),
                       max_attempts=int(limits_cfg["max_attempts"]))
    local_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    file_store = (S3FileStore.from_env(prefix=os.environ.get("HANDLER_FILE_PREFIX", ""))
                  if args.file_store == "s3" else LocalFileStore(data_dir / "files"))
    harnesses: dict = {}
    try:
        harnesses["codex"] = CodexHarness(
            codex_binary(),
            default_model=args.model,
            default_reasoning_effort=args.reasoning_effort,
            auth_json=Path(args.codex_auth_json).expanduser() if args.codex_auth_json else None,
            sandbox=args.codex_sandbox,
            auth_json_link=args.codex_auth_link,
        )
    except FileNotFoundError as exc:
        logger.warning("codex harness off: %s", exc)
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")
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
        tokens=tokens, mcp_url=f"http://{local_host}:{args.port}/mcp/",
        settings=settings, memory_tools=memory_tools,
        workspace_sync=workspace_sync,
    )
    dispatcher = Dispatcher(
        store, executor, lease_seconds=config.lease_seconds,
        max_concurrent=args.max_concurrent,
        lane_capacity={Lane.INTERACTIVE: int(limits_cfg["interactive_capacity"]),
                       Lane.BACKGROUND: int(limits_cfg["background_capacity"])},
    )

    auth = Authenticator(store, args.admin_key)
    if not args.admin_key:
        logger.warning("no admin key set; only user tokens can authenticate")
    uploads = UploadStore(data_dir / "uploads")
    app = create_app(store, bus, auth, default_harness=settings.harness_source("chat"),
                     uploads=uploads)
    app.mount("/mcp", mcp_app)
    app.include_router(approvals_router(gateway, store, auth))
    app.include_router(settings_router(settings, auth, memory_tools))
    app.include_router(admin_router(store, auth, scheduler=scheduler, gateway=gateway,
                                    uploads=uploads, log_file=_log_file(args)))
    webhook_secret = os.environ.get("HANDLER_WEBHOOK_SECRET")
    if webhook_secret:
        app.include_router(crontal_ingest_router(store, webhook_secret,
                                                 harness=settings.harness_source("crontal")))

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mcp_app.lifespan():
            yield

    app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port,
                                           log_config=None, access_log=False, lifespan="on"))
    server.install_signal_handlers = lambda: None  # we handle signals ourselves

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

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

    crontal_token = os.environ.get("CRONTAL_AGENT_TOKEN")
    if args.crontal_url and crontal_token:
        gateway.register(CrontalApiTools(
            crontal_base_url(args.crontal_url), crontal_token,
            workspace_for=lambda key: config.workspaces_dir / thread_slug(key)))

    async def start_crontal_connector() -> None:
        """Discover Crontal's tools (retrying until reachable), then expose them."""
        token = os.environ.get("CRONTAL_AGENT_TOKEN")
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

    tasks = [
        asyncio.create_task(start_crontal_connector(), name="crontal-connector"),
        asyncio.create_task(dispatcher.run_forever(stop), name="dispatcher"),
        asyncio.create_task(scheduler.run_forever(stop), name="scheduler"),
        asyncio.create_task(server.serve(), name="web"),
        asyncio.create_task(housekeeping(), name="housekeeping"),
        asyncio.create_task(workspace_sync.run_periodic(stop, args.sync_interval),
                            name="workspace-sync"),
    ]
    logger.info("handler-rt serving", extra={"url": f"http://{args.host}:{args.port}/login",
                                              "data_dir": str(data_dir),
                                              "tools": [s.name for s in gateway.specs()]})
    await stop.wait()
    # Heroku kills the dyno ~30 s after SIGTERM: upload unsynced workspaces
    # first, then wind down, and upload whatever in-flight runs touched.
    deadline = loop.time() + SHUTDOWN_BUDGET_SECONDS
    server.should_exit = True
    await _flush_until(workspace_sync, deadline - loop.time())
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                               timeout=max(deadline - loop.time() - 5, 1))
    except TimeoutError:
        logger.warning("shutdown: tasks still running at the deadline")
    await _flush_until(workspace_sync, deadline - loop.time())
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
    else:
        raise SystemExit(run_cli(args))


if __name__ == "__main__":
    main()
