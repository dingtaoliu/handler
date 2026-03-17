"""Agent: wraps the OpenAI Agents SDK to provide conversation-level reasoning.

Owns the inner reasoning loop — given a conversation's messages and system
prompt, it calls the LLM, executes tool calls, and returns the final response.
Also handles automatic compaction (summarizing old messages when input tokens
exceed a threshold) and per-turn token accounting.

The outer event loop (routing events to conversations) lives in Environment.
"""

import logging

from agents import Agent as OAIAgent, Runner, RunHooks
from agents.lifecycle import RunContextWrapper

from .actions.session import compact_messages
from .actions.watchdog import get_health_problems
from .context import AgentContext
from .event_store import EventStore
from .types import RunContext

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
        run_ctx: RunContext,
        tools: list | None = None,
        model: str = "gpt-5.4-2026-03-05",
        max_turns: int = 20,
        compact_token_threshold: int = 100_000,
        keep_recent: int = 10,
    ):
        self.store = store
        self.context = context
        self.run_ctx = run_ctx
        self.model = model
        self.max_turns = max_turns
        self.compact_token_threshold = compact_token_threshold
        self.keep_recent = keep_recent
        self.tools = list(tools or [])
        self._hooks = LoggingHooks()

    def _get_health_problems(self) -> list[str]:
        """Fetch health problems (best-effort). Called before building the prompt."""
        try:
            return get_health_problems()
        except Exception:
            return []

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
                await compact_messages(
                    self.store, conversation_id, active,
                    self.model, self.keep_recent,
                )

        logger.info(f"agent.run complete: {len(result.final_output)} chars")
        return result.final_output
