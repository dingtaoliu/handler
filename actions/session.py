"""Session and scheduling tools: compact_messages, compact_tool, cron_tools, stop_self."""

from __future__ import annotations

import logging
import signal
from typing import TYPE_CHECKING

from agents import function_tool
from openai import AsyncOpenAI

from ..types import RunContext
from ..utils import parse_interval as _parse_interval, next_run_from_now as _next_run_from_now

if TYPE_CHECKING:
    from ..event_store import EventStore

logger = logging.getLogger("handler.actions.session")


@function_tool
def stop_self() -> str:
    """Permanently stop the handler and disable the watchdog so it will not restart. To start again: python -m handler"""
    import os
    import threading

    from ..watchdog import remove_watchdog

    try:
        removed = remove_watchdog()
        watchdog_msg = "watchdog disabled" if removed else "watchdog was not configured"
    except Exception as e:
        watchdog_msg = f"watchdog removal failed (continuing anyway): {e}"

    pid = os.getpid()

    def _kill():
        os.kill(pid, signal.SIGTERM)

    threading.Timer(3.0, _kill).start()
    logger.info(f"stop_self: {watchdog_msg}, SIGTERM in 3s (pid={pid})")
    return (
        f"Stopping handler (PID {pid}): {watchdog_msg}. "
        "Process will exit in ~3 seconds and will NOT be restarted. "
        "To start again: python -m handler"
    )


async def compact_messages(
    store: "EventStore",
    conversation_id: str,
    messages: list[dict],
    model: str,
    keep_recent: int,
) -> int:
    """Summarize messages[:-keep_recent] via a bare LLM call and persist the result.

    Standalone function used by both the compact_conversation tool and
    Agent's auto-compaction logic, with no closure coupling to Agent.
    """
    to_compact = messages[:-keep_recent]
    if not to_compact:
        return 0

    existing_summary = store.get_latest_summary(conversation_id)

    parts = []
    if existing_summary:
        parts.append(f"## Prior Summary\n{existing_summary}")
    parts.append("## Conversation")
    for m in to_compact:
        parts.append(f"{m['role'].capitalize()}: {m['content']}")
    user_content = "\n\n".join(parts)

    system_prompt = (
        "You are a conversation summarizer. Produce a concise but complete summary that preserves:\n"
        "- All facts, decisions, and conclusions reached\n"
        "- Key numbers, dates, names, and values mentioned\n"
        "- Any tasks or follow-ups\n"
        "- The user's goals and context\n\n"
        "If a prior summary is provided, incorporate it so the output covers the full conversation history.\n"
        "Be dense and factual. This summary replaces the original messages in the context window."
    )

    logger.info(
        f"[compact] summarizing {len(to_compact)} messages "
        f"(prior summary: {'yes' if existing_summary else 'no'})"
    )
    client = AsyncOpenAI()
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    summary = response.choices[0].message.content or ""
    store.store_compaction(conversation_id, summary, len(to_compact))

    if response.usage:
        store.record_token_usage(
            conversation_id=conversation_id,
            model=model,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            trigger="compaction",
        )

    logger.info(
        f"[compact] done: {len(to_compact)} messages → {len(summary)} char summary"
    )
    return len(to_compact)


def compact_tool(
    store: "EventStore",
    run_ctx: RunContext,
    model: str,
    keep_recent: int,
):
    """Create a compact_conversation tool wired to the shared RunContext."""

    @function_tool
    async def compact_conversation() -> str:
        """Summarize and compress older messages to free up context space."""
        cid = run_ctx.conversation_id
        if not cid:
            return "No active conversation to compact."
        active = store.get_messages(cid)
        if len(active) <= keep_recent:
            return f"Not enough messages to compact (only {len(active)} active)."
        n = await compact_messages(store, cid, active, model, keep_recent)
        return f"Compacted {n} messages into a summary."

    return compact_conversation


def cron_tools(
    store: "EventStore",
    run_ctx: RunContext,
) -> list:
    """Create tools for the agent to manage cron jobs."""

    @function_tool
    def schedule_job(
        name: str,
        type: str,
        schedule: str,
        payload: str,
    ) -> str:
        """Schedule a recurring job. type is 'prompt' (send a message to yourself) or 'shell' (run a CLI command). schedule uses interval format: '30m', '2h', '1d', '30s'. payload is the prompt text or shell command."""
        if type not in ("prompt", "shell"):
            return f"Invalid type '{type}'. Must be 'prompt' or 'shell'."
        try:
            _parse_interval(schedule)
        except ValueError as e:
            return str(e)
        next_run = _next_run_from_now(schedule)
        cid = run_ctx.conversation_id or ""
        job_id = store.add_cron_job(
            name=name,
            type=type,
            schedule=schedule,
            next_run=next_run,
            payload=payload,
            conversation_id=cid if type == "prompt" else "",
        )
        return f"Scheduled job #{job_id} '{name}' ({type}, every {schedule}). Next run: {next_run} UTC."

    @function_tool
    def list_jobs() -> str:
        """List all scheduled cron jobs."""
        jobs = store.list_cron_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = []
        for j in jobs:
            status = "enabled" if j["enabled"] else "disabled"
            lines.append(
                f"#{j['id']} {j['name']} | {j['type']} | every {j['schedule']} | "
                f"{status} | next: {j['next_run']} | last: {j['last_run'] or 'never'}"
            )
        return "\n".join(lines)

    @function_tool
    def delete_job(job_id: int) -> str:
        """Delete a scheduled cron job by its ID."""
        if store.delete_cron_job(job_id):
            return f"Deleted job #{job_id}."
        return f"Job #{job_id} not found."

    return [schedule_job, list_jobs, delete_job]
