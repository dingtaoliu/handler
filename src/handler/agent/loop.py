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
from .base import BaseAgent
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
        max_turns: int = 20,
        compact_token_threshold: int = 100_000,
        keep_recent: int = 10,
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

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(self, name: str, tool_call_id: str, input_data: dict) -> str:
        tool = self._tool_lookup.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        try:
            return await invoke_tool(tool, name, tool_call_id, input_data)
        except Exception as e:
            logger.error(f"tool {name} raised: {e}", exc_info=True)
            return f"Error executing {name}: {e}"

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------

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

        # Hit max turns — return whatever we have
        return final_text, total_in, total_out

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compact_conversation(self, conversation_id: str) -> int:
        active = self.store.get_messages(conversation_id)
        if len(active) <= self.keep_recent:
            return 0
        return await self._provider.compact(
            self.store, conversation_id, active, self.keep_recent
        )

    async def run(self, conversation_id: str, messages: list[dict]) -> str:
        self.run_ctx.conversation_id = conversation_id
        self.run_ctx.user_id = self.store.get_conversation_user(conversation_id)
        summary = self.store.get_latest_summary(conversation_id)
        token_brief = self.store.get_token_cost_brief()
        instructions = self.context.build(
            summary=summary,
            token_brief=token_brief,
            user_id=self.run_ctx.user_id,
        )
        logger.info(
            f"agent.run: conversation={conversation_id}, messages={len(messages)}"
        )

        final_text, total_in, total_out = await self._agentic_loop(
            system=instructions,
            messages=messages,
            max_turns=self.max_turns,
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
            active = self.store.get_messages(conversation_id)
            if len(active) > self.keep_recent:
                logger.info(
                    f"[compact] auto-triggering: {total_in:,} input tokens "
                    f">= threshold {self.compact_token_threshold:,}"
                )
                await self._provider.compact(
                    self.store, conversation_id, active, self.keep_recent
                )

        logger.info(f"agent.run complete: {len(final_text)} chars")
        return final_text

    async def end_session(self, conversation_id: str) -> None:
        """Give the agent a chance to persist important info before the session is wiped."""
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
        instructions += (
            "\n\n# SESSION ENDING\n"
            "This session is about to end. Review the conversation and write anything "
            "important to memory files that hasn't been saved yet. Focus on:\n"
            "- Key facts, decisions, or conclusions\n"
            "- User preferences or corrections\n"
            "- Anything the user would expect you to remember next time\n\n"
            "If everything important is already in memory files, do nothing.\n"
            "Do NOT respond to the user — this is a background housekeeping step."
        )

        logger.info(
            f"[session] ending session {conversation_id}, "
            f"giving agent {len(messages)} messages to review"
        )

        try:
            _, total_in, total_out = await self._agentic_loop(
                system=instructions,
                messages=messages,
                max_turns=5,
            )
        except Exception as e:
            logger.error(f"[session] end_session failed: {e}", exc_info=True)
            total_in = total_out = 0

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
