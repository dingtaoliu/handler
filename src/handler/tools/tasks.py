"""Task tool: create and monitor autonomous background tasks."""

import logging
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from agents import function_tool

from ..event_store import EventStore
from ..types import RunContext

logger = logging.getLogger("handler.tools.tasks")


def task_tool(store: EventStore, run_ctx: RunContext, data_dir: Path):
    """Factory: returns a single 'task' tool for spawning background task agents."""

    @function_tool
    async def task(
        action: str,
        title: str = "",
        description: str = "",
        task_id: str = "",
    ) -> str:
        """Manage autonomous background tasks that run without blocking the conversation.

        Use 'create' to offload complex or time-consuming work to a background agent.
        The task agent has access to the same tools (files, shell, web search) and works
        autonomously until it calls task_complete(). You will be notified when it finishes.

        Actions:
        - create: Start a new background task. Required: title, description.
        - list:   Show all tasks and their current status.
        - check:  Read output and logs for a specific task. Required: task_id.
        - cancel: Stop a running task (SIGTERM). Required: task_id.

        Args:
            action:      One of: create, list, check, cancel.
            title:       Short name for the task (create only).
            description: Full instructions for the task agent — be specific about
                         what to do, what files/dirs to use, and what output is expected.
                         The task agent has no prior conversation context.
            task_id:     ID of a specific task (check/cancel only).
        """
        if action == "create":
            if not title or not description:
                return "Error: title and description are required for create."

            tid = uuid.uuid4().hex[:8]
            task_dir = data_dir / "tasks" / tid
            workspace_dir = task_dir / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)

            (task_dir / "task.md").write_text(f"# {title}\n\n{description}\n")

            cid = run_ctx.conversation_id or "web"
            channel = store.get_conversation_channel(cid)
            notify_channel = channel if channel == "telegram" else ""

            store.create_task(
                task_id=tid,
                title=title,
                description=description,
                conversation_id=cid,
                user_id=run_ctx.user_id or "",
                workspace_dir=str(workspace_dir),
                notify_channel=notify_channel,
            )

            log_path = task_dir / "log.txt"
            env = dict(os.environ)
            proc = subprocess.Popen(
                [sys.executable, "-m", "handler.task_runner", "--task-id", tid],
                stdout=open(str(log_path), "w"),
                stderr=subprocess.STDOUT,
                env=env,
            )
            store.update_task(tid, pid=proc.pid)

            logger.info(f"spawned task {tid} (pid={proc.pid}): {title}")
            return (
                f"Task '{title}' started (id={tid}, pid={proc.pid}).\n"
                f"Workspace: {workspace_dir}\n"
                f"You will be notified automatically when it completes."
            )

        elif action == "list":
            tasks = store.list_tasks()
            if not tasks:
                return "No tasks found."
            lines = []
            for t in tasks:
                hb = t.get("last_heartbeat") or "—"
                lines.append(
                    f"[{t['id']}] {t['title']}\n"
                    f"  status={t['status']}  pid={t['pid'] or '—'}  "
                    f"created={t['created_at']}  heartbeat={hb}"
                )
            return "\n".join(lines)

        elif action == "check":
            if not task_id:
                return "Error: task_id is required for check."
            t = store.get_task(task_id)
            if not t:
                return f"Task '{task_id}' not found."

            parts = [
                f"Task: {t['title']} (id={t['id']})",
                f"Status: {t['status']}",
                f"Created: {t['created_at']}",
            ]
            if t.get("started_at"):
                parts.append(f"Started: {t['started_at']}")
            if t.get("last_heartbeat"):
                parts.append(f"Last heartbeat: {t['last_heartbeat']}")
            if t.get("completed_at"):
                parts.append(f"Completed: {t['completed_at']}")
            if t.get("result"):
                parts.append(f"\nResult:\n{t['result']}")
            if t.get("error"):
                parts.append(f"\nError:\n{t['error']}")

            log_path = data_dir / "tasks" / task_id / "log.txt"
            if log_path.exists():
                lines = log_path.read_text().splitlines()
                tail = "\n".join(lines[-25:])
                parts.append(f"\nRecent log (last 25 lines):\n{tail}")

            return "\n".join(parts)

        elif action == "cancel":
            if not task_id:
                return "Error: task_id is required for cancel."
            t = store.get_task(task_id)
            if not t:
                return f"Task '{task_id}' not found."
            if t["status"] not in ("pending", "running"):
                return f"Task '{task_id}' is already {t['status']}."

            pid = t.get("pid")
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                    logger.info(f"sent SIGTERM to task {task_id} (pid={pid})")
                except ProcessLookupError:
                    pass
            store.update_task(task_id, status="cancelled")
            return f"Task '{task_id}' cancelled."

        else:
            return "Unknown action. Valid actions: create, list, check, cancel."

    return task
