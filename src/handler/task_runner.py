"""Lightweight background task runner.

Runs Agent.run() for a single task without starting any channels, web server,
or watchdog. Spawned as a subprocess by the task_tool in the main agent.

Usage:
    python -m handler.task_runner --task-id <id>
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("handler.task_runner")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Handler background task runner")
    p.add_argument("--task-id", required=True, help="Task ID to execute")
    return p.parse_args()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _run_task(task_id: str) -> None:
    from .paths import DATA_DIR, CONFIG_DIR
    from .event_store import EventStore
    from .types import RunContext
    from .agent import OpenAIAgent, ClaudeAgent
    from .tools import read_file, write_file, list_files, shell, web_search, search_codebase, edit_file

    store = EventStore(db_path=str(DATA_DIR / "handler.db"))

    task = store.get_task(task_id)
    if not task:
        logger.error(f"task {task_id} not found in database")
        return

    workspace_dir = Path(task["workspace_dir"])
    conversation_id = f"task:{task_id}"

    store.update_task(task_id, status="running", pid=os.getpid(), started_at=_now())
    store.ensure_conversation(conversation_id, channel="task")
    logger.info(f"starting task {task_id}: {task['title']}")

    task_system = (
        f"You are an autonomous background task agent. You run independently without user interaction.\n\n"
        f"Task: {task['title']}\n"
        f"Workspace directory: {workspace_dir}\n\n"
        f"Write all output files and results to your workspace directory.\n"
        f"When you have fully completed the task, call task_complete(result='...detailed summary...').\n"
        f"If you hit an unrecoverable error, call task_complete(result='ERROR: ...description...').\n"
        f"Do not ask questions or wait for input — work autonomously."
    )

    class _TaskContext:
        is_configured = True

        def build(self, summary=None, token_brief=None, user_id=None) -> str:
            return task_system

    run_ctx = RunContext()

    task_done: dict = {"completed": False, "result": None}

    from agents import function_tool

    @function_tool
    async def task_complete(result: str) -> str:
        """Signal that this background task is finished.

        Args:
            result: A detailed summary of what was accomplished, including key outputs,
                    file paths written, and any important findings. If the task failed,
                    start with 'ERROR: ' followed by the reason.
        """
        task_done["completed"] = True
        task_done["result"] = result
        store.update_task(
            task_id,
            status="completed",
            result=result,
            completed_at=_now(),
        )
        logger.info(f"task_complete called for {task_id}")
        return "Task marked complete. The main agent will be notified shortly."

    task_tools = [
        read_file,
        write_file,
        list_files,
        shell,
        web_search,
        search_codebase,
        edit_file,
        task_complete,
    ]

    # Load same backend/model as the main agent
    backend = "openai"
    model = "gpt-5.4-mini"
    agent_config_path = CONFIG_DIR / "agent.json"
    if agent_config_path.exists():
        try:
            cfg = json.loads(agent_config_path.read_text())
            backend = cfg.get("backend", backend)
            model = cfg.get("model", model)
        except Exception:
            pass

    ctx = _TaskContext()
    agent_kwargs = dict(
        context=ctx,
        store=store,
        run_ctx=run_ctx,
        tools=task_tools,
        model=model,
        max_turns=50,
    )
    if backend in ("claude", "anthropic"):
        agent = ClaudeAgent(**agent_kwargs)
    else:
        agent = OpenAIAgent(**agent_kwargs)

    task_prompt = (
        f"Complete the following task:\n\n"
        f"# {task['title']}\n\n"
        f"{task['description']}\n\n"
        f"Your workspace: {workspace_dir}\n\n"
        f"When done, call task_complete(result='...')."
    )
    store.add_message(conversation_id, "user", task_prompt)

    # Heartbeat loop — updates last_heartbeat every 60s so the main agent
    # can detect stalled tasks via task(action='check').
    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(60)
            store.update_task(task_id, last_heartbeat=_now())

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        messages = store.get_messages(conversation_id)
        response = await agent.run(conversation_id, messages)
        store.add_message(conversation_id, "assistant", response)
        store.update_task(task_id, last_heartbeat=_now())

        if not task_done["completed"]:
            # Agent finished its turns without calling task_complete — treat as done.
            store.update_task(
                task_id,
                status="completed",
                result=response[:4000],
                completed_at=_now(),
            )
            logger.info(f"task {task_id} finished (implicit completion)")
    except Exception as e:
        logger.error(f"task {task_id} failed: {e}", exc_info=True)
        store.update_task(
            task_id,
            status="failed",
            error=str(e),
            completed_at=_now(),
        )
    finally:
        heartbeat_task.cancel()


def main() -> None:
    args = _parse_args()
    task_id = args.task_id

    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s [task:{task_id[:8]}] %(message)s",
        stream=sys.stdout,
    )

    asyncio.run(_run_task(task_id))


if __name__ == "__main__":
    main()
