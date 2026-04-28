"""Agent ABC: defines the interface that all agent backends must implement."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..context import AgentContext
from ..event_store import EventStore
from ..types import (
    RunContext,
    extract_text_content,
    image_path_to_base64_url,
    messages_to_openai,
    messages_to_openai_responses,
    messages_to_anthropic,
)

logger = logging.getLogger("handler.agent.base")

# Re-export so existing ``from .base import ...`` in sibling modules still works.
__all__ = [
    "BaseAgent",
    "extract_text_content",
    "image_path_to_base64_url",
    "messages_to_openai",
    "messages_to_openai_responses",
    "messages_to_anthropic",
]

_SESSION_ENDING_SUFFIX = (
    "\n\n# SESSION ENDING\n"
    "This session is about to end. Review the conversation and write anything "
    "important to memory files that hasn't been saved yet. Focus on:\n"
    "- Key facts, decisions, or conclusions\n"
    "- User preferences or corrections\n"
    "- Anything the user would expect you to remember next time\n\n"
    "If everything important is already in memory files, do nothing.\n"
    "Do NOT respond to the user — this is a background housekeeping step."
)


class BaseAgent(ABC):
    """Abstract base for agent backends (OpenAI, Claude, etc.).

    Subclasses own the inner reasoning loop via _inner_run(). The outer event
    loop lives in Environment. run() and end_session() are template methods —
    they handle context setup, token recording, and compaction; subclasses only
    implement _inner_run() and compact_conversation().
    """

    def __init__(
        self,
        context: AgentContext,
        store: EventStore,
        run_ctx: RunContext,
        tools: list | None = None,
        model: str = "",
        max_turns: int | None = 50,
        compact_token_threshold: int = 100_000,
        keep_recent: int = 10,
    ):
        self.context = context
        self.store = store
        self.run_ctx = run_ctx
        self.model = model
        self.max_turns = max_turns
        self.compact_token_threshold = compact_token_threshold
        self.keep_recent = keep_recent
        self.tools = list(tools or [])

    @abstractmethod
    async def _inner_run(
        self,
        system: str,
        messages: list[dict],
        max_turns: int | None,
    ) -> tuple[str, int, int]:
        """Run the inner LLM ↔ tool loop.

        Returns (final_text, input_tokens, output_tokens).
        """
        ...

    @abstractmethod
    async def compact_conversation(self, conversation_id: str) -> int:
        """Compact older messages for a conversation and return the number compacted."""
        ...

    async def run(self, conversation_id: str, messages: list[dict]) -> str:
        """Run the agent on a conversation and return the final text response."""
        self.run_ctx.conversation_id = conversation_id
        self.run_ctx.user_id = self.store.get_conversation_user(conversation_id)
        instructions = self.context.build(
            summary=self.store.get_latest_summary(conversation_id),
            token_brief=self.store.get_token_cost_brief(),
            user_id=self.run_ctx.user_id,
        )
        logger.info(
            f"agent.run: conversation={conversation_id}, messages={len(messages)}"
        )
        logger.debug(f"[system_prompt]\n{instructions}\n[/system_prompt]")

        final_text, total_in, total_out = await self._inner_run(
            instructions, messages, self.max_turns
        )

        self.store.record_token_usage(
            conversation_id=conversation_id,
            model=self.model,
            input_tokens=total_in,
            output_tokens=total_out,
            trigger="chat",
        )
        self.store.log_event(
            "agent_run",
            "agent",
            {"conversation_id": conversation_id, "input_messages": len(messages)},
            conversation_id,
            self.run_ctx.user_id or "",
        )

        if total_in >= self.compact_token_threshold:
            logger.info(
                f"[compact] auto-triggering: {total_in:,} input tokens "
                f">= threshold {self.compact_token_threshold:,}"
            )
            await self.compact_conversation(conversation_id)

        logger.info(f"agent.run complete: {len(final_text)} chars")
        return final_text

    async def end_session(self, conversation_id: str) -> None:
        """Persist important info to memory before the session is wiped."""
        messages = self.store.get_messages(conversation_id)
        if not messages:
            return

        self.run_ctx.conversation_id = conversation_id
        self.run_ctx.user_id = self.store.get_conversation_user(conversation_id)
        instructions = self.context.build(
            summary=self.store.get_latest_summary(conversation_id),
            token_brief=self.store.get_token_cost_brief(),
            user_id=self.run_ctx.user_id,
        )
        instructions += _SESSION_ENDING_SUFFIX

        logger.info(
            f"[session] ending session {conversation_id}, "
            f"giving agent {len(messages)} messages to review"
        )

        total_in = total_out = 0
        try:
            _, total_in, total_out = await self._inner_run(instructions, messages, max_turns=5)
        except Exception as e:
            logger.error(f"[session] end_session failed: {e}", exc_info=True)

        if total_in > 0 or total_out > 0:
            self.store.record_token_usage(
                conversation_id=conversation_id,
                model=self.model,
                input_tokens=total_in,
                output_tokens=total_out,
                trigger="end_session",
            )

        n = self.store.compact_all(conversation_id)
        logger.info(
            f"[session] session {conversation_id} ended, compacted {n} messages"
        )
