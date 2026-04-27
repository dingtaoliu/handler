"""Claude Agent: uses the Claude Agent SDK for the agentic loop.

Drop-in replacement for OpenAIAgent — same constructor signature and public
methods (run, end_session).  Custom handler tools are exposed via an in-process
MCP server; the SDK handles the tool-calling loop automatically.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import anthropic

from claude_agent_sdk import (
    query as sdk_query,
    ClaudeAgentOptions,
    create_sdk_mcp_server,
    tool as sdk_tool,
)
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

from ..context import AgentContext
from ..event_store import EventStore
from ..types import RunContext
from .base import BaseAgent, extract_text_content
from .tools import invoke_tool, _is_function_tool, _clean_schema

logger = logging.getLogger("handler.agent.claude")


# ---------------------------------------------------------------------------
# MCP tool adapter: wrap handler tools for the Claude Agent SDK
# ---------------------------------------------------------------------------


def _extract_defaults(tool_obj: Any) -> dict[str, Any]:
    """Extract parameter defaults from a tool's params_json_schema."""
    props = tool_obj.params_json_schema.get("properties", {})
    return {
        name: prop["default"]
        for name, prop in props.items()
        if "default" in prop
    }


def _make_mcp_tool(tool_obj: Any):
    """Convert a handler Tool or OpenAI FunctionTool to a Claude Agent SDK MCP tool."""
    name = tool_obj.name
    desc = tool_obj.description
    defaults = _extract_defaults(tool_obj)

    # Build schema with optional params removed from required
    schema = _clean_schema(tool_obj.params_json_schema)
    if defaults and "required" in schema:
        schema = dict(schema)
        schema["required"] = [p for p in schema["required"] if p not in defaults]

    async def _fn(args: dict[str, Any]) -> dict[str, Any]:
        # Fill in any missing optional params with their defaults
        full_args = {**defaults, **args}
        try:
            result = await invoke_tool(tool_obj, name, "sdk-call", full_args)
            return {"content": [{"type": "text", "text": result}]}
        except Exception as e:
            logger.error(f"tool {name} raised: {e}", exc_info=True)
            return {
                "content": [{"type": "text", "text": f"Error executing {name}: {e}"}],
                "isError": True,
            }

    return sdk_tool(name, desc, schema)(_fn)


# ---------------------------------------------------------------------------
# Claude-based compaction (uses raw Anthropic API — not the SDK)
# ---------------------------------------------------------------------------


COMPACTION_MODEL = "claude-haiku-4-5-20251001"


async def _compact_with_claude(
    client: anthropic.AsyncAnthropic,
    store: EventStore,
    conversation_id: str,
    messages: list[dict],
    keep_recent: int,
) -> int:
    """Summarize messages[:-keep_recent] using the Claude API and persist."""
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

    response = await client.messages.create(
        model=COMPACTION_MODEL,
        max_tokens=16384,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    summary = next(
        (block.text for block in response.content if block.type == "text"), ""
    )
    store.store_compaction(conversation_id, summary, len(to_compact))

    store.record_token_usage(
        conversation_id=conversation_id,
        model=COMPACTION_MODEL,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        trigger="compaction",
    )

    logger.info(
        f"[compact] done: {len(to_compact)} messages → {len(summary)} char summary"
    )
    return len(to_compact)


# ---------------------------------------------------------------------------
# ClaudeAgent
# ---------------------------------------------------------------------------


class ClaudeAgent(BaseAgent):
    """Agent backend using the Claude Agent SDK."""

    MCP_SERVER_NAME = "handler"

    def __init__(
        self,
        context: AgentContext,
        store: EventStore,
        run_ctx: RunContext,
        tools: list | None = None,
        model: str = "claude-opus-4-6",
        max_turns: int | None = 50,
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
        # Raw Anthropic client kept for compaction (simple summarization call)
        self._client = anthropic.AsyncAnthropic()

        # Build MCP server from handler tools
        mcp_tools = []
        self._tool_names: list[str] = []
        for t in self.tools:
            if _is_function_tool(t):
                mcp_tools.append(_make_mcp_tool(t))
                self._tool_names.append(t.name)

        self._mcp_server = create_sdk_mcp_server(
            name=self.MCP_SERVER_NAME,
            tools=mcp_tools,
        )
        logger.info(
            f"MCP server created with {len(mcp_tools)} tools: {self._tool_names}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def compact_conversation(self, conversation_id: str) -> int:
        active = self.store.get_messages(conversation_id)
        if len(active) <= self.keep_recent:
            return 0
        return await _compact_with_claude(
            self._client,
            self.store,
            conversation_id,
            active,
            self.keep_recent,
        )

    def _allowed_tools(self) -> list[str]:
        """Build the allowed_tools list with the MCP naming convention.

        Includes the built-in Read tool so the agent can view image files
        referenced in multi-modal messages.
        """
        tools = [f"mcp__{self.MCP_SERVER_NAME}__{n}" for n in self._tool_names]
        tools.append("Read")  # built-in: view image files
        return tools

    @staticmethod
    def _format_messages_as_prompt(messages: list[dict]) -> str:
        """Format conversation history into a single prompt for the SDK.

        The SDK's query() takes a single prompt string, so we encode
        prior turns as structured context and leave the latest user
        message as the actual prompt. Image content blocks are converted
        to file path references and the agent is instructed to view them
        via the Read tool for full visual understanding.
        """
        if not messages:
            return ""

        # Collect image paths from recent user messages so we can instruct the
        # agent to view them (the SDK only accepts text prompts).
        image_paths: list[str] = []
        for m in messages:
            content = m["content"]
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "image" and block.get("path"):
                        image_paths.append(block["path"])

        if len(messages) == 1:
            prompt = extract_text_content(messages[0]["content"])
        else:
            # Previous messages become conversation context
            history_lines = []
            for m in messages[:-1]:
                role = m["role"].capitalize()
                history_lines.append(f"[{role}]: {extract_text_content(m['content'])}")
            history = "\n\n".join(history_lines)

            latest = extract_text_content(messages[-1]["content"])

            prompt = (
                f"<conversation_history>\n{history}\n</conversation_history>\n\n"
                f"{latest}"
            )

        # If images are present, add an explicit instruction to view them
        if image_paths:
            paths_list = "\n".join(f"- {p}" for p in image_paths)
            prompt += (
                f"\n\n<important>The user has attached image(s). "
                f"Use the Read tool to view each image file before responding — "
                f"do not guess from the filename alone:\n{paths_list}</important>"
            )

        return prompt

    def _build_options(self, system: str, max_turns: int | None) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions for an SDK query."""
        return ClaudeAgentOptions(
            system_prompt=system,
            model=self.model,
            max_turns=max_turns,
            thinking={"type": "adaptive"},
            mcp_servers={self.MCP_SERVER_NAME: self._mcp_server},
            allowed_tools=self._allowed_tools(),
            permission_mode="bypassPermissions",
        )

    async def _run_sdk(
        self,
        system: str,
        messages: list[dict],
        max_turns: int | None = None,
    ) -> tuple[str, int, int]:
        """Run the Claude Agent SDK loop.

        Returns (final_text, total_input_tokens, total_output_tokens).
        """
        prompt = self._format_messages_as_prompt(messages)
        options = self._build_options(system, max_turns)

        final_text = ""
        last_assistant_text = ""
        total_in = 0
        total_out = 0

        async for message in sdk_query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                parts = [b.text for b in message.content if isinstance(b, TextBlock)]
                if parts:
                    last_assistant_text = "".join(parts)

            if isinstance(message, ResultMessage):
                final_text = message.result if message.result else last_assistant_text
                if message.usage:
                    total_in = message.usage.get("input_tokens", 0)
                    total_out = message.usage.get("output_tokens", 0)
                logger.info(
                    f"[sdk] turns={message.num_turns}, "
                    f"cost=${message.total_cost_usd:.4f}, "
                    f"tokens: in={total_in:,}, out={total_out:,}"
                )

        # Strip SDK citation markers (e.g. citeturn0fetch0, citeturn1search2)
        final_text = re.sub(r"cite\w+", "", final_text).strip()

        return final_text, total_in, total_out

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, conversation_id: str, messages: list[dict]) -> str:
        self.run_ctx.conversation_id = conversation_id
        summary = self.store.get_latest_summary(conversation_id)
        token_brief = self.store.get_token_cost_brief()
        instructions = self.context.build(
            summary=summary,
            token_brief=token_brief,
        )
        logger.info(f"[system_prompt]\n{instructions}\n[/system_prompt]")
        logger.info(
            f"agent.run: conversation={conversation_id}, messages={len(messages)}"
        )

        final_text, total_in, total_out = await self._run_sdk(
            system=instructions,
            messages=messages,
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
                await _compact_with_claude(
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

        logger.info(
            f"[session] ending session {conversation_id}, "
            f"giving agent {len(messages)} messages to review"
        )

        total_in = total_out = 0
        try:
            _, total_in, total_out = await self._run_sdk(
                system=instructions,
                messages=messages,
            )
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
