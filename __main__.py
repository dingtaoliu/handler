"""Handler entry point. Run with: python -m handler"""

import asyncio
import logging
import os
import signal

from dotenv import load_dotenv

from .agent import Agent
from .context import AgentContext
from .event_store import EventStore
from .memory import Memory
from .environment import Environment
from .paths import DATA_DIR, CONFIG_DIR, MEMORY_DIR, PID_PATH, LOG_DIR, get_log_path
from .types import RunContext
from .channels import WebChannel, TelegramChannel, SchedulerChannel, gmail_tools
from .actions import (
    read_file,
    write_file,
    write_and_run,
    web_search,
    run_python,
    run_shell,
    compact_tool,
    cron_tools,
    mark_stable,
    restart_self,
    stop_self,
    write_core_file,
    memory_tools,
    search_codebase,
    patch_file,
    read_source,
)
from .watchdog import install_watchdog, load_scheduler_config, detect_scheduler_backends

load_dotenv()

logger = logging.getLogger("handler")

MODEL = "gpt-5.4-2026-03-05"
KEEP_RECENT = 10


def _write_pid() -> None:
    PID_PATH.write_text(str(os.getpid()))


def _remove_pid() -> None:
    try:
        PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
_file_handler = logging.FileHandler(
    get_log_path(),
    mode="a",
    encoding="utf-8",
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
)
logging.getLogger().addHandler(_file_handler)


def main():
    _write_pid()

    # Auto-detect and install watchdog if not already configured
    config = load_scheduler_config()
    if config:
        try:
            install_watchdog(config["backend"])
        except Exception as e:
            logger.warning(f"watchdog install failed (non-fatal): {e}")
    else:
        try:
            result = detect_scheduler_backends()
            recommendation = result.get("recommendation")
            if recommendation and recommendation != "none":
                install_watchdog(recommendation)
                logger.info(f"watchdog auto-configured: backend='{recommendation}'")
            else:
                logger.info("no suitable watchdog backend found — running without watchdog")
        except Exception as e:
            logger.warning(f"watchdog auto-detection failed (non-fatal): {e}")

    mem = Memory(memory_dir=MEMORY_DIR)
    context = AgentContext(config_dir=CONFIG_DIR, memory_dir=MEMORY_DIR, memory=mem)
    store = EventStore(db_path=str(DATA_DIR / "handler.db"))
    run_ctx = RunContext()

    tools = [
        compact_tool(store, run_ctx, model=MODEL, keep_recent=KEEP_RECENT),
        read_file,
        write_file,
        write_and_run,
        web_search,
        run_python,
        run_shell,
        mark_stable,
        restart_self,
        stop_self,
        write_core_file,
        search_codebase,
        patch_file,
        read_source,
    ]

    tools.extend(cron_tools(store, run_ctx))
    tools.extend(memory_tools(mem))

    # Gmail tools (requires credentials/desktop.json + token.json)
    try:
        tools.extend(gmail_tools())
        print("Gmail tools loaded")
    except Exception as e:
        print(f"Gmail tools not available: {e}")

    agent = Agent(
        context=context,
        store=store,
        run_ctx=run_ctx,
        tools=tools,
        model=MODEL,
        keep_recent=KEEP_RECENT,
    )

    env = Environment(agent, store)
    env.add_channel(WebChannel(store, memory=mem, config_dir=CONFIG_DIR, tools=tools))
    env.add_channel(SchedulerChannel(store))

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if telegram_token:
        env.add_channel(TelegramChannel(telegram_token))

    if context.is_configured:
        print("Starting Handler at http://localhost:8000")
    else:
        print("Starting Handler (first run — onboarding) at http://localhost:8000")

    asyncio.run(_run(env))


async def _run(env: Environment) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    env_task = asyncio.create_task(env.run())
    await stop.wait()
    logger.info("Shutdown signal received, stopping...")
    env_task.cancel()
    try:
        await env_task
    except asyncio.CancelledError:
        pass
    finally:
        _remove_pid()


if __name__ == "__main__":
    main()
