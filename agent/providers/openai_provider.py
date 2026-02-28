"""
OpenAI provider implementation.
"""

import json
import logging
from typing import Any, Iterator, Optional

from .model_provider import (
    ModelProvider,
    ProviderResponse,
    ProviderStreamEvent,
    ToolCall,
)

logger = logging.getLogger(__name__)


class OpenAIProvider(ModelProvider):
    """LLM provider backed by the OpenAI API."""

    def __init__(self, model: str, api_key: str, **kwargs: Any) -> None:
        """
        Args:
            model: OpenAI model identifier, e.g. "gpt-5-nano-2025-08-07"
            api_key: OpenAI API key
            **kwargs: Additional options (e.g. base_url for Azure/proxy)
        """
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai package is required: pip install openai")

        self.model = model
        self._client = OpenAI(
            api_key=api_key, **{k: v for k, v in kwargs.items() if k == "base_url"}
        )

    # Reasoning models (o1, o3 family) only support temperature=1 (the default)
    # and raise a 400 error if any other value is sent.
    _REASONING_PREFIXES = ("o1", "o3")

    def _supports_temperature(self) -> bool:
        """Return False for OpenAI reasoning models that reject temperature."""
        return not self.model.startswith(self._REASONING_PREFIXES)

    def generate(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> ProviderResponse:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if temperature is not None and self._supports_temperature():
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout

        logger.debug(
            "OpenAI generate: model=%s, messages=%d, tools=%d",
            self.model,
            len(messages),
            len(tools or []),
        )
        response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    arguments = {}
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=arguments)
                )

        return ProviderResponse(
            content=msg.content,
            tool_calls=tool_calls,
            tokens_in=response.usage.prompt_tokens if response.usage else None,
            tokens_out=response.usage.completion_tokens if response.usage else None,
            model=response.model,
            raw=response,
        )

    def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Iterator[ProviderStreamEvent]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if temperature is not None and self._supports_temperature():
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout

        logger.debug(
            "OpenAI generate_stream: model=%s, messages=%d, tools=%d",
            self.model,
            len(messages),
            len(tools or []),
        )

        # Accumulators for the streamed response
        content_parts: list[str] = []
        # tool_calls_raw: index → {id, name, arguments (partial JSON string)}
        tool_calls_raw: dict[int, dict[str, str]] = {}
        usage = None
        model_name: Optional[str] = None

        for chunk in self._client.chat.completions.create(**kwargs):
            # Usage arrives on the final empty chunk when stream_options.include_usage=True
            if chunk.usage:
                usage = chunk.usage

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            if chunk.model:
                model_name = chunk.model

            # Text delta
            if delta.content:
                content_parts.append(delta.content)
                yield {"type": "text_delta", "content": delta.content}

            # Tool call deltas — accumulate fragments by index
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_raw:
                        tool_calls_raw[idx] = {"id": "", "name": "", "arguments": ""}
                    raw = tool_calls_raw[idx]
                    if tc_delta.id:
                        raw["id"] = tc_delta.id
                    if tc_delta.function and tc_delta.function.name:
                        raw["name"] = tc_delta.function.name
                    if tc_delta.function and tc_delta.function.arguments:
                        raw["arguments"] += tc_delta.function.arguments

        # Build structured ToolCall objects from accumulated raw data
        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_calls_raw.keys()):
            raw = tool_calls_raw[idx]
            try:
                arguments = json.loads(raw["arguments"]) if raw["arguments"] else {}
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                ToolCall(id=raw["id"], name=raw["name"], arguments=arguments)
            )

        response = ProviderResponse(
            content="".join(content_parts) or None,
            tool_calls=tool_calls,
            tokens_in=usage.prompt_tokens if usage else None,
            tokens_out=usage.completion_tokens if usage else None,
            model=model_name or self.model,
        )
        yield {"type": "done", "response": response}

    def get_model_name(self) -> str:
        return self.model

    def supports_streaming(self) -> bool:
        return True

    def supports_tools(self) -> bool:
        return True
