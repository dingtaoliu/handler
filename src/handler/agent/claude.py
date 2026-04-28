"""Claude Agent: uses the Claude Agent SDK for the agentic loop.

Drop-in replacement for OpenAIAgent — same constructor signature and public
methods (run, end_session).  Custom handler tools are exposed via an in-process
MCP server; the SDK handles the tool-calling loop automatically.
"""

from __future__ import annotations

import logging
import re
from typing import Any

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
from .providers.anthropic import AnthropicProvider, COMPACTION_MODEL
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
        self._compactor = AnthropicProvider(COMPACTION_MODEL)

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

    async def compact_conversation(self, conversation_id: str) -> int:
        active = self.store.get_messages(conversation_id)
        if len(active) <= self.keep_recent:
            return 0
        return await self._compactor.compact(
            self.store, conversation_id, active, self.keep_recent
        )

    def _allowed_tools(self) -> list[str]:
        """Build the allowed_tools list with the MCP naming convention.

        Includes the built-in Read tool so the agent can view image files
        referenced in multi-modal messages.
        """
        tools = [f"mcp__{self.MCP_SERVER_NAME}__{n}" for n in self._tool_names]
        tools.append("Read")
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

        if image_paths:
            paths_list = "\n".join(f"- {p}" for p in image_paths)
            prompt += (
                f"\n\n<important>The user has attached image(s). "
                f"Use the Read tool to view each image file before responding — "
                f"do not guess from the filename alone:\n{paths_list}</important>"
            )

        return prompt

    def _build_options(self, system: str, max_turns: int | None) -> ClaudeAgentOptions:
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

        final_text = re.sub(r"cite\w+", "", final_text).strip()
        return final_text, total_in, total_out

    async def _inner_run(
        self,
        system: str,
        messages: list[dict],
        max_turns: int | None,
    ) -> tuple[str, int, int]:
        return await self._run_sdk(system, messages, max_turns)
