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
from .gateway.gateway import Gateway
from .gateway.mcp_server import create_mcp_app
from .gateway.policy import GrantPolicy
from .gateway.tokens import RunTokens
from .harness.codex import SANDBOX_MODES, CodexHarness, codex_binary
from .memory import MemoryTools
from .observability.logging import configure_json_logging
from .observability.pricing import PriceTable
from .run import RunConfig, RunExecutor
from .scheduler import Scheduler
from .settings import SettingsService
from .store.postgres import PostgresStore
from .store.sqlite import SQLiteStore
from .tools.automation import AutomationTools
from .tools.notify import NotifyTools
from .types import Event, Lane, ThreadSpec
from .web.app import create_app
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
    s.add_argument("--codex-sandbox",
                   default=os.environ.get("HANDLER_CODEX_SANDBOX", "workspace-write"),
                   choices=SANDBOX_MODES,
                   help="Codex sandbox mode; use danger-full-access only where the container "
                        "itself is the isolation boundary (e.g. Heroku dynos)")
    s.add_argument("--admin-key", default=os.environ.get("HANDLER_ADMIN_KEY"),
                   help="instance admin key (bearer credential with admin rights)")
    s.add_argument("--log-level", default="INFO")
    add_cli_commands(sub)
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

    settings = SettingsService(store)
    await settings.seed_from_files(
        Path(args.handler_yaml).expanduser() if args.handler_yaml else None,
        Path(args.agent_md).expanduser() if args.agent_md else None,
    )
    threads_cfg, _ = await settings.get("threads")
    limits_cfg, _ = await settings.get("limits")
    grants_cfg, grants_version = await settings.get("grants")

    emit_event = thread_router(store)

    users = [u.strip() for u in args.users.split(",") if u.strip()]
    memory_tools = MemoryTools(store)
    scheduler = Scheduler(store, default_harness=threads_cfg["automation"]["harness"])
    gateway = Gateway(store, GrantPolicy(grants_cfg), emit_event=emit_event)
    for provider in (memory_tools, AutomationTools(store, scheduler),
                     NotifyTools(store, bus, users_provider=lambda: users)):
        gateway.register(provider)
    tokens = RunTokens()
    mcp_app = create_mcp_app(gateway, tokens)

    harness = CodexHarness(
        codex_binary(),
        default_model=args.model,
        default_reasoning_effort=args.reasoning_effort,
        auth_json=Path(args.codex_auth_json).expanduser() if args.codex_auth_json else None,
        sandbox=args.codex_sandbox,
    )
    config = RunConfig(data_dir=data_dir, reasoning_effort=args.reasoning_effort,
                       timeout_seconds=float(limits_cfg["run_timeout_seconds"]),
                       max_attempts=int(limits_cfg["max_attempts"]))
    local_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    file_store = (S3FileStore.from_env(prefix=os.environ.get("HANDLER_FILE_PREFIX", ""))
                  if args.file_store == "s3" else LocalFileStore(data_dir / "files"))
    executor = RunExecutor(
        store, {"codex": harness}, bus, config, PriceTable.from_yaml(),
        tokens=tokens, mcp_url=f"http://{local_host}:{args.port}/mcp/",
        settings=settings, memory_tools=memory_tools,
        workspace_sync=WorkspaceSync(file_store, data_dir),
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
    app = create_app(store, bus, auth, default_harness=threads_cfg["chat"]["harness"])
    app.mount("/mcp", mcp_app)
    app.include_router(approvals_router(gateway, store, auth))
    app.include_router(settings_router(settings, auth))

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

    tasks = [
        asyncio.create_task(dispatcher.run_forever(stop), name="dispatcher"),
        asyncio.create_task(scheduler.run_forever(stop), name="scheduler"),
        asyncio.create_task(server.serve(), name="web"),
        asyncio.create_task(housekeeping(), name="housekeeping"),
    ]
    logger.info("handler-rt serving", extra={"url": f"http://{args.host}:{args.port}/login",
                                              "data_dir": str(data_dir),
                                              "tools": [s.name for s in gateway.specs()]})
    await stop.wait()
    server.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)
    await store.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.command == "serve":
        configure_json_logging(args.log_level)
        asyncio.run(serve(args))
    else:
        raise SystemExit(run_cli(args))


if __name__ == "__main__":
    main()
