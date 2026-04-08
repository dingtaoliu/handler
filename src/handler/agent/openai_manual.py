"""OpenAI Manual Agent: manual agentic loop using the OpenAI Chat Completions API.

Drop-in replacement for OpenAIAgent — same constructor signature and public
methods (run, end_session). Uses a manual tool-calling loop instead of the
OpenAI Agents SDK Runner, mirroring ClaudeAgent's architecture.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from openai import AsyncOpenAI
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall,
)
from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

from ..tools.watchdog import get_health_problems
from ..context import AgentContext
from ..event_store import EventStore
from ..types import RunContext
from .base import BaseAgent, messages_to_openai, extract_text_content
from .tools import build_tool_defs_for_openai, invoke_tool

logger = logging.getLogger("handler.agent.openai_manual")


def _assistant_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for part in content:
        text = getattr(part, "text", None)
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(part, dict):
            dict_text = part.get("text")
            if isinstance(dict_text, str):
                parts.append(dict_text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# OpenAI-based compaction
# ---------------------------------------------------------------------------


async def _compact_with_openai(
    client: AsyncOpenAI,
    store: EventStore,
    conversation_id: str,
    messages: list[dict],
    model: str,
    keep_recent: int,
) -> int:
    """Summarize messages[:-keep_recent] using the OpenAI API and persist."""
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


# ---------------------------------------------------------------------------
# OpenAIManualAgent
# ---------------------------------------------------------------------------


class OpenAIManualAgent(BaseAgent):
    """Agent backend using the OpenAI Chat Completions API with a manual agentic loop."""

    def __init__(
        self,
        context: AgentContext,
        store: EventStore,
        run_ctx: RunContext,
        tools: list | None = None,
        model: str = "gpt-5.4-2026-03-05",
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
        self._client = AsyncOpenAI()
        self._tool_defs, self._tool_lookup = build_tool_defs_for_openai(self.tools)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def _get_health_problems(self) -> list[str]:
        try:
            return get_health_problems()
        except Exception:
            return []

    async def compact_conversation(self, conversation_id: str) -> int:
        active = self.store.get_messages(conversation_id)
        if len(active) <= self.keep_recent:
            return 0
        return await _compact_with_openai(
            self._client,
            self.store,
            conversation_id,
            active,
            self.model,
            self.keep_recent,
        )

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(
        self, name: str, tool_call_id: str, input_data: dict
    ) -> str:
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
        messages: list[ChatCompletionMessageParam],
        max_turns: int,
    ) -> tuple[str, int, int]:
        """Run the OpenAI manual agentic loop.

        Returns (final_text, total_input_tokens, total_output_tokens).
        """
        total_in = 0
        total_out = 0
        final_text = ""

        # Prepend system message
        api_messages: list[ChatCompletionMessageParam] = [
            cast(ChatCompletionMessageParam, {"role": "system", "content": system}),
            *messages,
        ]

        for turn in range(max_turns):
            if self._tool_defs:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    messages=api_messages,
                    tools=cast(list[ChatCompletionToolParam], self._tool_defs),
                )
            else:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    messages=api_messages,
                )

            choice = response.choices[0]
            msg = choice.message
            final_text = _assistant_message_text(msg.content)

            if response.usage:
                total_in += response.usage.prompt_tokens
                total_out += response.usage.completion_tokens
                logger.info(
                    f"[tokens] turn {turn}: in={response.usage.prompt_tokens:,}, "
                    f"out={response.usage.completion_tokens:,} | "
                    f"run total: in={total_in:,}, out={total_out:,}"
                )

            # If the model is done or not asking for tools → return text
            if choice.finish_reason != "tool_calls":
                return final_text, total_in, total_out

            # Handle tool calls
            # Append assistant message with tool calls to history
            api_messages.append(
                cast(ChatCompletionMessageParam, msg.model_dump(exclude_none=True))
            )

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    if not isinstance(tc, ChatCompletionMessageFunctionToolCall):
                        logger.warning(
                            f"skipping unsupported tool call type: {tc.type}"
                        )
                        continue

                    function_call = tc.function
                    logger.info(f"[action] {function_call.name} called")
                    try:
                        input_data = json.loads(function_call.arguments)
                    except json.JSONDecodeError:
                        input_data = {}
                    result = await self._execute_tool(
                        function_call.name, tc.id, input_data
                    )
                    preview = result[:200].replace("\n", " ") if result else ""
                    logger.info(f"[action] {function_call.name} returned: {preview}")
                    api_messages.append(
                        cast(
                            ChatCompletionMessageParam,
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result,
                            },
                        )
                    )

        # Hit max turns — return whatever we have
        return final_text, total_in, total_out

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, conversation_id: str, messages: list[dict]) -> str:
        self.run_ctx.conversation_id = conversation_id
        summary = self.store.get_latest_summary(conversation_id)
        token_brief = self.store.get_token_cost_brief()
        health_problems = self._get_health_problems()
        instructions = self.context.build(
            summary=summary,
            token_brief=token_brief,
            health_problems=health_problems,
        )
        logger.info(
            f"agent.run: conversation={conversation_id}, messages={len(messages)}"
        )

        # Convert stored messages (with image refs) to OpenAI multi-modal format
        oai_messages = messages_to_openai(messages)

        final_text, total_in, total_out = await self._agentic_loop(
            system=instructions,
            messages=oai_messages,
            max_turns=self.max_turns,
        )

        # Record token usage
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
            {
                "conversation_id": conversation_id,
                "input_messages": len(messages),
            },
        )

        # Auto-compact if needed
        if total_in >= self.compact_token_threshold:
            active = self.store.get_messages(conversation_id)
            if len(active) > self.keep_recent:
                logger.info(
                    f"[compact] auto-triggering: {total_in:,} input tokens "
                    f">= threshold {self.compact_token_threshold:,}"
                )
                await _compact_with_openai(
                    self._client,
                    self.store,
                    conversation_id,
                    active,
                    self.model,
                    self.keep_recent,
                )

        logger.info(f"agent.run complete: {len(final_text)} chars")
        return final_text

    async def end_session(self, conversation_id: str) -> None:
        """Give the agent a chance to persist important info before the session is wiped."""
        messages = self.store.get_messages(conversation_id)
        if not messages:
            return

        self.run_ctx.conversation_id = conversation_id
        instructions = self.context.build(
            summary=self.store.get_latest_summary(conversation_id),
            token_brief=self.store.get_token_cost_brief(),
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

        oai_messages = messages_to_openai(messages)

        logger.info(
            f"[session] ending session {conversation_id}, "
            f"giving agent {len(messages)} messages to review"
        )

        try:
            _, total_in, total_out = await self._agentic_loop(
                system=instructions,
                messages=oai_messages,
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
