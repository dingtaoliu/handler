"""Anthropic Messages API provider."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

import anthropic

from ...types import messages_to_anthropic
from ..tools import build_tool_defs_for_claude
from .base import ModelProvider, ToolCall, LLMResponse, COMPACTION_SYSTEM, build_compaction_prompt

if TYPE_CHECKING:
    from ...event_store import EventStore

logger = logging.getLogger("handler.agent.providers.anthropic")

COMPACTION_MODEL = "claude-haiku-4-5-20251001"


class AnthropicProvider(ModelProvider):
    """Adapts the Anthropic Messages API."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._client = anthropic.AsyncAnthropic()

    @property
    def compaction_model(self) -> str:
        return COMPACTION_MODEL

    def build_tool_defs(self, tools: list) -> tuple[list, dict[str, Any]]:
        return build_tool_defs_for_claude(tools)

    def format_messages(self, messages: list[dict]) -> list:
        return messages_to_anthropic(messages)

    async def chat(self, system: str, messages: list, tool_defs: list) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "system": system,
            "messages": messages,
            "max_tokens": 8096,
        }
        if tool_defs:
            kwargs["tools"] = tool_defs

        response = await self._client.messages.create(**kwargs)

        text = ""
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text = block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, input=block.input)
                )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            _native=response,
        )

    def append_assistant_turn(self, messages: list, response: LLMResponse) -> None:
        native = response._native
        content_blocks = []
        for block in native.content:
            if block.type == "text":
                content_blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        messages.append({"role": "assistant", "content": content_blocks})

    def append_tool_results(
        self, messages: list, tool_calls: list[ToolCall], results: list[str]
    ) -> None:
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tc.id, "content": result}
                    for tc, result in zip(tool_calls, results)
                ],
            }
        )

    async def compact(
        self,
        store: "EventStore",
        conversation_id: str,
        messages: list[dict],
        keep_recent: int,
    ) -> int:
        to_compact, user_content = build_compaction_prompt(
            store, conversation_id, messages, keep_recent
        )
        if not to_compact:
            return 0

        logger.info(f"[compact] summarizing {len(to_compact)} messages")

        response = await self._client.messages.create(
            model=COMPACTION_MODEL,
            max_tokens=16384,
            system=COMPACTION_SYSTEM,
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

        logger.info(f"[compact] done: {len(to_compact)} messages → {len(summary)} char summary")
        return len(to_compact)
