"""Handler entry point. Run with: python -m handler"""

import asyncio
import json
import logging
import os
import signal

from dotenv import load_dotenv

from .agent import BaseAgent, OpenAIAgent, ClaudeAgent, ManualAgent
from .agent.providers import OpenAIProvider, AnthropicProvider
from .context import AgentContext
from .event_store import EventStore
from .memory import Memory
from .environment import Environment
from .paths import (
    DATA_DIR,
    CONFIG_DIR,
    MEMORY_DIR,
    PID_PATH,
    LOG_DIR,
    ensure_scripts_dir_on_path,
    get_log_path,
)
from .types import RunContext
from .users import bootstrap_user_layout
from .channels import WebChannel, TelegramChannel, SchedulerChannel
from .tools import (
    read_file,
    write_file,
    list_files,
    shell,
    web_search,
    compact_tool,
    cron_tool,
    memory_tool,
    search_codebase,
    edit_file,
    gmail_tool,
    gdrive_tool,
)
from .watchdog import install_watchdog, load_scheduler_config, detect_scheduler_backends

ensure_scripts_dir_on_path()

load_dotenv(DATA_DIR / ".env")  # workspace env (~/.handler/.env)
load_dotenv()  # fallback: search from cwd upward

logger = logging.getLogger("handler")

KEEP_RECENT = 10

_VALID_BACKENDS = {"openai", "openai-manual", "claude", "anthropic"}

_DEFAULT_MODELS = {
    "openai": "gpt-5.4-mini",
    "openai-manual": "gpt-5.4-mini",
    "claude": "claude-opus-4-6",
    "anthropic": "claude-opus-4-6",
}

_MANUAL_PROVIDERS = {
    "openai-manual": OpenAIProvider,
    "anthropic": AnthropicProvider,
}

_AGENT_CONFIG_PATH = CONFIG_DIR / "agent.json"


def _normalize_agent_config(backend: str | None, model: str | None) -> dict[str, str]:
    resolved_backend = (backend or os.environ.get("HANDLER_AGENT", "openai")).strip()
    if resolved_backend not in _VALID_BACKENDS:
        resolved_backend = "openai"

    resolved_model = (model or "").strip() or _DEFAULT_MODELS[resolved_backend]
    return {"backend": resolved_backend, "model": resolved_model}


def _load_agent_config() -> dict:
    """Load agent backend/model from config file, falling back to env var."""
    if _AGENT_CONFIG_PATH.exists():
        try:
            data = json.loads(_AGENT_CONFIG_PATH.read_text())
            return _normalize_agent_config(
                data.get("backend"),
                data.get("model"),
            )
        except Exception:
            pass
    return _normalize_agent_config(None, None)


def _save_agent_config(backend: str, model: str) -> None:
    """Persist agent config to disk."""
    config = _normalize_agent_config(backend, model)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _AGENT_CONFIG_PATH.write_text(json.dumps(config, indent=2))


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
                logger.info(
                    "no suitable watchdog backend found — running without watchdog"
                )
        except Exception as e:
            logger.warning(f"watchdog auto-detection failed (non-fatal): {e}")

    bootstrap_user_layout()
    mem = Memory(memory_dir=MEMORY_DIR)
    context = AgentContext(config_dir=CONFIG_DIR, memory_dir=MEMORY_DIR, memory=mem)
    store = EventStore(db_path=str(DATA_DIR / "handler.db"))
    run_ctx = RunContext()

    agent_cfg = _load_agent_config()
    backend = agent_cfg["backend"]
    model = agent_cfg["model"]
    agent_ref: dict[str, BaseAgent | None] = {"current": None}

    def _get_agent() -> BaseAgent:
        agent = agent_ref["current"]
        if agent is None:
            raise RuntimeError("agent not initialized")
        return agent

    tools = [
        compact_tool(run_ctx, _get_agent),
        read_file,
        write_file,
        list_files,
        shell,
        web_search,
        search_codebase,
        edit_file,
        cron_tool(store, run_ctx),
    ]

    tools.append(memory_tool(mem, run_ctx))

    tools.append(gmail_tool(run_ctx))
    print("Gmail tool loaded")

    tools.append(gdrive_tool(run_ctx))
    print("Google Drive tool loaded")

    def _build_agent(b: str, m: str) -> BaseAgent:
        kwargs = dict(
            context=context,
            store=store,
            run_ctx=run_ctx,
            tools=tools,
            model=m,
            keep_recent=KEEP_RECENT,
        )
        if b == "claude":
            return ClaudeAgent(**kwargs)
        if b in _MANUAL_PROVIDERS:
            return ManualAgent(provider=_MANUAL_PROVIDERS[b](m), **kwargs)
        return OpenAIAgent(**kwargs)

    agent = _build_agent(backend, model)
    agent_ref["current"] = agent
    env = Environment(agent, store)

    def swap_agent(new_backend: str, new_model: str) -> None:
        """Rebuild the agent and hot-swap it on the environment."""
        _save_agent_config(new_backend, new_model)
        next_agent = _build_agent(new_backend, new_model)
        agent_ref["current"] = next_agent
        env.agent = next_agent
        logger.info(f"agent swapped: backend={new_backend}, model={new_model}")

    env.add_channel(
        WebChannel(
            store,
            memory=mem,
            config_dir=CONFIG_DIR,
            tools=tools,
            agent_config_loader=_load_agent_config,
            agent_swapper=swap_agent,
        )
    )
    env.add_channel(SchedulerChannel(store))

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if telegram_token:
        allowed_raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "").strip()
        allowed_ids = (
            {int(uid) for uid in allowed_raw.split(",") if uid.strip()}
            if allowed_raw
            else None
        )
        env.add_channel(TelegramChannel(telegram_token, allowed_user_ids=allowed_ids))

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
