"""Session and scheduling tools: compact_messages, compact_tool, and cron_tool."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Protocol

from agents import function_tool
from openai import AsyncOpenAI

from ..types import RunContext, extract_text_content
from ..utils import (
    parse_interval as _parse_interval,
    next_run_from_now as _next_run_from_now,
)

if TYPE_CHECKING:
    from ..event_store import EventStore

logger = logging.getLogger("handler.tools.session")


COMPACTION_MODEL = "gpt-4o-mini"


class _CompactingAgent(Protocol):
    async def compact_conversation(self, conversation_id: str) -> int: ...


async def compact_messages(
    store: "EventStore",
    conversation_id: str,
    messages: list[dict],
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
        parts.append(f"{m['role'].capitalize()}: {extract_text_content(m['content'])}")
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
        model=COMPACTION_MODEL,
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
            model=COMPACTION_MODEL,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            trigger="compaction",
        )

    logger.info(
        f"[compact] done: {len(to_compact)} messages → {len(summary)} char summary"
    )
    return len(to_compact)


def compact_tool(
    run_ctx: RunContext,
    agent_getter: Callable[[], _CompactingAgent],
):
    """Create a compact_conversation tool wired to the active agent backend."""

    @function_tool
    async def compact_conversation() -> str:
        """Summarize and compress older messages to free up context space."""
        cid = run_ctx.conversation_id
        if not cid:
            return "No active conversation to compact."
        agent = agent_getter()
        n = await agent.compact_conversation(cid)
        if n <= 0:
            return "Not enough messages to compact."
        return f"Compacted {n} messages into a summary."

    return compact_conversation


def cron_tool(
    store: "EventStore",
    run_ctx: RunContext,
):
    """Create a single cron tool for the agent to manage scheduled jobs."""

    @function_tool
    def cron(
        action: str,
        name: str = "",
        type: str = "",
        schedule: str = "",
        payload: str = "",
        notify_channel: str = "",
        job_id: int = 0,
    ) -> str:
        """Manage scheduled jobs. Actions: create, list, delete, help.

        Args:
            action:         One of: create, list, delete, help.
            name:           (create) Job name.
            type:           (create) 'prompt' or 'shell'.
            schedule:       (create) Interval: '30m', '2h', '1d', '30s'.
            payload:        (create) Prompt text or shell command.
            notify_channel: (create) Where to push results: 'web', 'telegram', or '' (none).
            job_id:         (delete) ID of the job to delete.
        """
        if action == "help":
            return (
                "cron — manage scheduled jobs\n\n"
                "Actions:\n"
                "  create  — Schedule a recurring job.\n"
                "            Required: name, type ('prompt'|'shell'), schedule ('30m','2h','1d'), payload.\n"
                "            Optional: notify_channel ('web'|'telegram'|'').\n"
                "  list    — List all scheduled jobs.\n"
                "  delete  — Delete a job by ID. Required: job_id.\n"
            )

        if action == "list":
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

        if action == "create":
            if not all([name, type, schedule, payload]):
                return "Missing required fields. Need: name, type, schedule, payload."
            if type not in ("prompt", "shell"):
                return f"Invalid type '{type}'. Must be 'prompt' or 'shell'."
            if notify_channel and notify_channel not in ("web", "telegram"):
                return f"Invalid notify_channel '{notify_channel}'. Must be 'web', 'telegram', or empty."
            try:
                _parse_interval(schedule)
            except ValueError as e:
                return str(e)
            next_run = _next_run_from_now(schedule)
            cid = run_ctx.conversation_id or ""
            jid = store.add_cron_job(
                name=name,
                type=type,
                schedule=schedule,
                next_run=next_run,
                payload=payload,
                conversation_id=cid if type == "prompt" else "",
                notify_channel=notify_channel if type == "prompt" else "",
            )
            notify_msg = f", notify via {notify_channel}" if notify_channel else ""
            return f"Scheduled job #{jid} '{name}' ({type}, every {schedule}{notify_msg}). Next run: {next_run} UTC."

        if action == "delete":
            if not job_id:
                return "Missing required field: job_id."
            if store.delete_cron_job(job_id):
                return f"Deleted job #{job_id}."
            return f"Job #{job_id} not found."

        return f"Unknown action '{action}'. Use: create, list, delete, help."

    return cron
