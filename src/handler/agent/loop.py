"""Provider-agnostic manual agentic loop.

ManualAgent drives the LLM ↔ tool-call loop directly, without an SDK runner.
The provider abstraction (ModelProvider) handles all API-specific formatting,
so the loop itself is identical regardless of whether the underlying model is
OpenAI or Anthropic.
"""

from __future__ import annotations

import logging

from ..context import AgentContext
from ..event_store import EventStore
from ..types import RunContext
from .base import BaseAgent, DEFAULT_MAX_TURNS, DEFAULT_COMPACT_THRESHOLD, DEFAULT_KEEP_RECENT
from .providers.base import ModelProvider
from .tools import invoke_tool

logger = logging.getLogger("handler.agent.loop")


class ManualAgent(BaseAgent):
    """Agent backend using a provider-agnostic manual agentic loop."""

    def __init__(
        self,
        provider: ModelProvider,
        context: AgentContext,
        store: EventStore,
        run_ctx: RunContext,
        tools: list | None = None,
        model: str = "",
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
        self._provider = provider
        self._tool_defs, self._tool_lookup = provider.build_tool_defs(self.tools)

    async def _execute_tool(self, name: str, tool_call_id: str, input_data: dict) -> str:
        tool = self._tool_lookup.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        try:
            return await invoke_tool(tool, name, tool_call_id, input_data)
        except Exception as e:
            logger.error(f"tool {name} raised: {e}", exc_info=True)
            return f"Error executing {name}: {e}"

    async def _agentic_loop(
        self,
        system: str,
        messages: list[dict],
        max_turns: int,
    ) -> tuple[str, int, int]:
        """Run the provider-agnostic agentic loop.

        Returns (final_text, total_input_tokens, total_output_tokens).
        """
        api_messages = self._provider.format_messages(messages)
        total_in = 0
        total_out = 0
        final_text = ""

        for turn in range(max_turns):
            response = await self._provider.chat(system, api_messages, self._tool_defs)
            total_in += response.input_tokens
            total_out += response.output_tokens
            final_text = response.text

            logger.info(
                f"[tokens] turn {turn}: in={response.input_tokens:,}, "
                f"out={response.output_tokens:,} | "
                f"run total: in={total_in:,}, out={total_out:,}"
            )

            if response.done:
                return final_text, total_in, total_out

            self._provider.append_assistant_turn(api_messages, response)

            results = []
            for tc in response.tool_calls:
                logger.info(f"[action] {tc.name} called")
                result = await self._execute_tool(tc.name, tc.id, tc.input)
                preview = result[:200].replace("\n", " ") if result else ""
                logger.info(f"[action] {tc.name} returned: {preview}")
                results.append(result)

            self._provider.append_tool_results(api_messages, response.tool_calls, results)

        return final_text, total_in, total_out

    async def _inner_run(
        self,
        system: str,
        messages: list[dict],
        max_turns: int | None,
    ) -> tuple[str, int, int]:
        turns = max_turns if max_turns is not None else self.max_turns or DEFAULT_MAX_TURNS
        return await self._agentic_loop(system, messages, turns)

    async def compact_conversation(self, conversation_id: str) -> int:
        active = self.store.get_messages(conversation_id)
        if len(active) <= self.keep_recent:
            return 0
        return await self._provider.compact(
            self.store, conversation_id, active, self.keep_recent
        )
