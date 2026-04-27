"""OpenAI Chat Completions provider."""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

from openai import AsyncOpenAI

from ...types import extract_text_content, messages_to_openai
from ..tools import build_tool_defs_for_openai
from .base import ModelProvider, ToolCall, LLMResponse

if TYPE_CHECKING:
    from ...event_store import EventStore

logger = logging.getLogger("handler.agent.providers.openai")

COMPACTION_MODEL = "gpt-4o-mini"

_COMPACTION_SYSTEM = (
    "You are a conversation summarizer. Produce a concise but complete summary that preserves:\n"
    "- All facts, decisions, and conclusions reached\n"
    "- Key numbers, dates, names, and values mentioned\n"
    "- Any tasks or follow-ups\n"
    "- The user's goals and context\n\n"
    "If a prior summary is provided, incorporate it so the output covers the full conversation history.\n"
    "Be dense and factual. This summary replaces the original messages in the context window."
)


class OpenAIProvider(ModelProvider):
    """Adapts the OpenAI Chat Completions API."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._client = AsyncOpenAI()

    @property
    def compaction_model(self) -> str:
        return COMPACTION_MODEL

    def build_tool_defs(self, tools: list) -> tuple[list, dict[str, Any]]:
        return build_tool_defs_for_openai(tools)

    def format_messages(self, messages: list[dict]) -> list:
        return list(messages_to_openai(messages))

    async def chat(self, system: str, messages: list, tool_defs: list) -> LLMResponse:
        api_messages = [{"role": "system", "content": system}, *messages]
        kwargs: dict[str, Any] = {"model": self.model, "messages": api_messages}
        if tool_defs:
            kwargs["tools"] = tool_defs

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        # Extract text
        content = msg.content
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(p.text for p in content if hasattr(p, "text"))
        else:
            text = ""

        # Extract tool calls
        tool_calls: list[ToolCall] = []
        if choice.finish_reason == "tool_calls" and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.type == "function":
                    try:
                        input_data = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        input_data = {}
                    tool_calls.append(
                        ToolCall(id=tc.id, name=tc.function.name, input=input_data)
                    )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
            _native=msg,
        )

    def append_assistant_turn(self, messages: list, response: LLMResponse) -> None:
        messages.append(response._native.model_dump(exclude_none=True))

    def append_tool_results(
        self, messages: list, tool_calls: list[ToolCall], results: list[str]
    ) -> None:
        for tc, result in zip(tool_calls, results):
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    async def compact(
        self,
        store: "EventStore",
        conversation_id: str,
        messages: list[dict],
        keep_recent: int,
    ) -> int:
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

        logger.info(
            f"[compact] summarizing {len(to_compact)} messages "
            f"(prior summary: {'yes' if existing_summary else 'no'})"
        )

        response = await self._client.chat.completions.create(
            model=COMPACTION_MODEL,
            messages=[
                {"role": "system", "content": _COMPACTION_SYSTEM},
                {"role": "user", "content": user_content},
            ],
        )
        summary = response.choices[0].message.content or ""
        store.store_compaction(conversation_id, summary, len(to_compact))

        if response.usage:
            store.record_token_usage(
                conversation_id=conversation_id,
                model=COMPACTION_MODEL,
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                trigger="compaction",
            )

        logger.info(
            f"[compact] done: {len(to_compact)} messages → {len(summary)} char summary"
        )
        return len(to_compact)
