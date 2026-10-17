"""`handler-rt serve`: the new runtime in one process (web API + dispatcher)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

import uvicorn

from .bus import RunEventBus
from .dispatcher import Dispatcher
from .harness.codex import CodexHarness, codex_binary
from .observability.logging import configure_json_logging
from .observability.pricing import PriceTable
from .run import RunConfig, RunExecutor
from .store.sqlite import SQLiteStore
from .web.app import create_app

logger = logging.getLogger("handler.runtime.server")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="handler-rt", description="Handler runtime (rebuild)")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve", help="run the web API and dispatcher")
    s.add_argument("--data-dir", default=os.environ.get("HANDLER_RT_DATA_DIR", "~/.handler-rt"))
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8100)
    s.add_argument("--model", default=os.environ.get("HANDLER_RT_MODEL"))
    s.add_argument("--reasoning-effort", default=os.environ.get("HANDLER_RT_EFFORT", "low"))
    s.add_argument("--codex-auth-json", default=os.environ.get("HANDLER_RT_CODEX_AUTH"),
                   help="auth.json copied into each isolated CODEX_HOME (dev only)")
    s.add_argument("--instructions-file", default=None)
    s.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


async def serve(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(data_dir / "handler.db")
    bus = RunEventBus()
    instructions = Path(args.instructions_file).read_text() if args.instructions_file else ""
    config = RunConfig(data_dir=data_dir, instructions=instructions,
                       reasoning_effort=args.reasoning_effort)
    harness = CodexHarness(
        codex_binary(),
        default_model=args.model,
        default_reasoning_effort=args.reasoning_effort,
        auth_json=Path(args.codex_auth_json).expanduser() if args.codex_auth_json else None,
    )
    executor = RunExecutor(store, {"codex": harness}, bus, config, PriceTable.from_yaml())
    dispatcher = Dispatcher(store, executor, lease_seconds=config.lease_seconds)

    app = create_app(store, bus, default_harness="codex")
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port,
                                           log_config=None, lifespan="off"))
    server.install_signal_handlers = lambda: None  # we handle signals ourselves

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    dispatch_task = asyncio.create_task(dispatcher.run_forever(stop))
    web_task = asyncio.create_task(server.serve())
    logger.info("handler-rt serving", extra={"url": f"http://{args.host}:{args.port}/timeline",
                                              "data_dir": str(data_dir)})
    await stop.wait()
    server.should_exit = True
    await asyncio.gather(dispatch_task, web_task, return_exceptions=True)
    await store.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    configure_json_logging(args.log_level)
    if args.command == "serve":
        asyncio.run(serve(args))


if __name__ == "__main__":
    main()
