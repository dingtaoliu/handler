"""
Dummy provider for testing without a real LLM API key.

Returns scripted responses and can simulate tool calls.
"""

import logging
from typing import Any, Iterator, Optional

from .model_provider import (
    ModelProvider,
    ProviderResponse,
    ProviderStreamEvent,
    ToolCall,
)

logger = logging.getLogger(__name__)


class DummyProvider(ModelProvider):
    """
    Stub LLM provider for local development and testing.

    Cycles through a list of preset responses. Can be configured to
    return tool calls to test the full agent loop.
    """

    def __init__(
        self,
        model: str = "dummy",
        api_key: str = "",
        responses: Optional[list[str]] = None,
        tool_calls: Optional[list[ToolCall]] = None,
        **kwargs: Any,
    ) -> None:
        """
        Args:
            model: Dummy model name
            api_key: Ignored
            responses: List of text responses to cycle through
            tool_calls: If set, the first generate() call returns these tool
                        calls instead of a text response; subsequent calls
                        return the normal responses
        """
        self.model = model
        self._responses = responses or ["This is a dummy response from the agent."]
        self._tool_calls_once = tool_calls or []
        self._call_count = 0

    def generate(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> ProviderResponse:
        logger.debug("DummyProvider.generate call #%d", self._call_count)

        # On the first call, return any configured tool calls
        if self._call_count == 0 and self._tool_calls_once:
            self._call_count += 1
            return ProviderResponse(
                content=None,
                tool_calls=self._tool_calls_once,
                tokens_in=10,
                tokens_out=5,
                model=self.model,
            )

        text = self._responses[self._call_count % len(self._responses)]
        self._call_count += 1
        return ProviderResponse(
            content=text,
            tool_calls=[],
            tokens_in=10,
            tokens_out=len(text.split()),
            model=self.model,
        )

    def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Iterator[ProviderStreamEvent]:
        response = self.generate(messages, tools, temperature, max_tokens)
        if response.content:
            for word in response.content.split(" "):
                yield {"type": "text_delta", "content": word + " "}
        yield {"type": "done", "response": response}

    def get_model_name(self) -> str:
        return self.model

    def supports_streaming(self) -> bool:
        return True

    def supports_tools(self) -> bool:
        return False
