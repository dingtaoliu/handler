import logging

from agents import Agent as OAIAgent, Runner, RunHooks
from agents.lifecycle import RunContextWrapper
from openai import AsyncOpenAI

from .actions import compact_tool
from .context import AgentContext
from .event_store import EventStore

logger = logging.getLogger("handler.agent")


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


class Agent:
    def __init__(
        self,
        context: AgentContext,
        store: EventStore,
        tools: list | None = None,
        model: str = "gpt-5.4-2026-03-05",
        max_turns: int = 20,
        compact_token_threshold: int = 100_000,
        keep_recent: int = 10,
    ):
        self.store = store
        self.context = context
        self.model = model
        self.max_turns = max_turns
        self.compact_token_threshold = compact_token_threshold
        self.keep_recent = keep_recent
        self._current_conversation_id: str | None = None
        self._hooks = LoggingHooks()

        comp_tool = compact_tool(
            store,
            get_conversation_id=lambda: self._current_conversation_id,
            keep_recent=keep_recent,
            do_compact=lambda cid, msgs: self._compact(cid, msgs),
        )
        self.tools = [comp_tool] + (tools or [])

    async def _compact(self, conversation_id: str, messages: list[dict]) -> int:
        """Summarize messages[:-keep_recent] via a bare LLM call and persist the result."""
        to_compact = messages[: -self.keep_recent]
        if not to_compact:
            return 0

        existing_summary = self.store.get_latest_summary(conversation_id)

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
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        summary = response.choices[0].message.content or ""
        self.store.store_compaction(conversation_id, summary, len(to_compact))

        # Record compaction token usage
        if response.usage:
            self.store.record_token_usage(
                conversation_id=conversation_id,
                model=self.model,
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                trigger="compaction",
            )

        logger.info(
            f"[compact] done: {len(to_compact)} messages → {len(summary)} char summary"
        )
        return len(to_compact)

    async def end_session(self, conversation_id: str) -> None:
        """Give the agent a chance to persist important info before the session is wiped."""
        messages = self.store.get_messages(conversation_id)
        if not messages:
            return

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

        agent = OAIAgent(
            name="handler",
            instructions=instructions,
            tools=self.tools,
            model=self.model,
        )

        logger.info(
            f"[session] ending session {conversation_id}, "
            f"giving agent {len(messages)} messages to review"
        )
        self._hooks.reset()
        try:
            await Runner.run(
                agent,
                input=messages,
                max_turns=5,
                hooks=self._hooks,
            )
        except Exception as e:
            logger.error(f"[session] end_session failed: {e}", exc_info=True)

        # Record end_session token usage
        if self._hooks._prev_input > 0 or self._hooks._prev_output > 0:
            self.store.record_token_usage(
                conversation_id=conversation_id,
                model=self.model,
                input_tokens=self._hooks._prev_input,
                output_tokens=self._hooks._prev_output,
                trigger="end_session",
            )

        # Compact everything
        n = self.store.compact_all(conversation_id)
        logger.info(
            f"[session] session {conversation_id} ended, compacted {n} messages"
        )

    async def run(self, conversation_id: str, messages: list[dict]) -> str:
        self._current_conversation_id = conversation_id
        summary = self.store.get_latest_summary(conversation_id)
        token_brief = self.store.get_token_cost_brief()
        instructions = self.context.build(summary=summary, token_brief=token_brief)
        logger.info(
            f"agent.run: conversation={conversation_id}, messages={len(messages)}"
        )

        agent = OAIAgent(
            name="handler",
            instructions=instructions,
            tools=self.tools,
            model=self.model,
        )

        self._hooks.reset()
        try:
            result = await Runner.run(
                agent,
                input=messages,
                max_turns=self.max_turns,
                hooks=self._hooks,
            )
        except Exception as e:
            logger.error(f"agent.run failed: {e}", exc_info=True)
            raise

        self.store.log_event(
            "agent_run",
            "agent",
            {
                "conversation_id": conversation_id,
                "input_messages": len(messages),
            },
        )

        # Record token usage
        self.store.record_token_usage(
            conversation_id=conversation_id,
            model=self.model,
            input_tokens=self._hooks._prev_input,
            output_tokens=self._hooks._prev_output,
            trigger="chat",
        )

        if self._hooks._prev_input >= self.compact_token_threshold:
            active = self.store.get_messages(conversation_id)
            if len(active) > self.keep_recent:
                logger.info(
                    f"[compact] auto-triggering: {self._hooks._prev_input:,} input tokens "
                    f">= threshold {self.compact_token_threshold:,}"
                )
                await self._compact(conversation_id, active)

        logger.info(f"agent.run complete: {len(result.final_output)} chars")
        return result.final_output
