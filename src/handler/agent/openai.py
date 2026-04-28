"""OpenAI Agent: wraps the OpenAI Agents SDK to provide conversation-level reasoning.

Owns the inner reasoning loop — given a conversation's messages and system
prompt, it calls the LLM, executes tool calls, and returns the final response.
Also handles automatic compaction (summarizing old messages when input tokens
exceed a threshold) and per-turn token accounting.

The outer event loop (routing events to conversations) lives in Environment.
"""

import logging
from typing import cast

from agents import Agent as OAIAgent, Runner, RunHooks
from agents.items import TResponseInputItem
from agents.run_context import RunContextWrapper

from ..tools.session import compact_messages
from ..context import AgentContext
from ..event_store import EventStore
from ..types import RunContext
from .base import BaseAgent, messages_to_openai_responses, DEFAULT_MAX_TURNS, DEFAULT_COMPACT_THRESHOLD, DEFAULT_KEEP_RECENT

logger = logging.getLogger("handler.agent.openai")


class LoggingHooks(RunHooks):
    """Logs tool calls and LLM token usage per turn."""

    def __init__(self) -> None:
        self._prev_input = 0
        self._prev_output = 0

    def reset(self) -> None:
        self._prev_input = 0
        self._prev_output = 0

    async def on_tool_start(
        self, context: RunContextWrapper, agent: OAIAgent, tool
    ) -> None:
        logger.info(f"[action] {tool.name} called")

    async def on_tool_end(
        self, context: RunContextWrapper, agent: OAIAgent, tool, result: str
    ) -> None:
        preview = result[:200].replace("\n", " ") if result else ""
        logger.info(f"[action] {tool.name} returned: {preview}")

    async def on_llm_end(
        self, context: RunContextWrapper, agent: OAIAgent, response
    ) -> None:
        u = context.usage
        delta_in = u.input_tokens - self._prev_input
        delta_out = u.output_tokens - self._prev_output
        self._prev_input = u.input_tokens
        self._prev_output = u.output_tokens
        logger.info(
            f"[tokens] turn: in={delta_in:,}, out={delta_out:,} | "
            f"run total: in={u.input_tokens:,}, out={u.output_tokens:,}, total={u.total_tokens:,}"
        )


class OpenAIAgent(BaseAgent):
    def __init__(
        self,
        context: AgentContext,
        store: EventStore,
        run_ctx: RunContext,
        tools: list | None = None,
        model: str = "gpt-5.4-mini",
        max_turns: int = DEFAULT_MAX_TURNS,
        compact_token_threshold: int = DEFAULT_COMPACT_THRESHOLD,
        keep_recent: int = DEFAULT_KEEP_RECENT,
    ):
        super().__init__(
            context=context,
            store=store,
            run_ctx=run_ctx,
            tools=tools,
            model=model,
            max_turns=max_turns,
            compact_token_threshold=compact_token_threshold,
            keep_recent=keep_recent,
        )
        self._hooks = LoggingHooks()

    async def _inner_run(
        self,
        system: str,
        messages: list[dict],
        max_turns: int | None,
    ) -> tuple[str, int, int]:
        agent = OAIAgent(
            name="handler",
            instructions=system,
            tools=self.tools,
            model=self.model,
        )
        self._hooks.reset()
        input_items = cast(
            list[TResponseInputItem],
            messages_to_openai_responses(messages),
        )
        result = await Runner.run(
            agent,
            input=input_items,
            max_turns=max_turns,
            hooks=self._hooks,
        )
        return result.final_output, self._hooks._prev_input, self._hooks._prev_output

    async def compact_conversation(self, conversation_id: str) -> int:
        active = self.store.get_messages(conversation_id)
        if len(active) <= self.keep_recent:
            return 0
        return await compact_messages(
            self.store,
            conversation_id,
            active,
            self.keep_recent,
        )
