"""Handler entry point. Run with: python -m handler"""

import asyncio
import logging
import logging.handlers
import os
import signal
from pathlib import Path

from dotenv import load_dotenv

from .agent import Agent
from .context import AgentContext
from .event_store import EventStore
from .memory import Memory
from .environment import Environment
from .channels import WebChannel, TelegramChannel, SchedulerChannel, gmail_tools
from .actions import (
    read_file,
    write_file,
    write_and_run,
    web_search,
    run_python,
    run_shell,
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

_PID_PATH = None  # set in main() after DATA_DIR is known


def _write_pid() -> None:
    _PID_PATH.write_text(str(os.getpid()))


def _remove_pid() -> None:
    try:
        _PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass


_PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = _PACKAGE_DIR / "data"
CONFIG_DIR = DATA_DIR / "config"
MEMORY_DIR = DATA_DIR / "memory"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    DATA_DIR / "handler.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
)
logging.getLogger().addHandler(_file_handler)


def main():
    global _PID_PATH
    _PID_PATH = DATA_DIR / "handler.pid"
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

    tools = [
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

    # Cron tools need a reference to the agent's current conversation_id,
    # which is set during Agent.run(). We wire them up via a lambda that
    # reads from the agent instance — but agent doesn't exist yet, so we
    # use a mutable container.
    _agent_ref: list[Agent | None] = [None]

    def _get_cid() -> str | None:
        return _agent_ref[0] and _agent_ref[0]._current_conversation_id

    tools.extend(cron_tools(store, get_conversation_id=_get_cid))
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
        tools=tools,
    )
    _agent_ref[0] = agent

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
