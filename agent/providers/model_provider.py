"""
Abstract base class for LLM providers.

All configuration is passed at init time. Providers never read environment
variables directly — that is the responsibility of the Flask app factory.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


@dataclass
class ToolCall:
    """A single tool call requested by the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ProviderResponse:
    """Normalized response from any LLM provider."""

    content: Optional[str]
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    model: Optional[str] = None
    raw: Any = None  # original provider response object, for debugging


# ── Stream event types ─────────────────────────────────────────────────────────
# generate_stream() yields one of two event shapes:
#   {"type": "text_delta", "content": str}           — partial text chunk
#   {"type": "done",       "response": ProviderResponse} — final complete response
ProviderStreamEvent = dict[str, Any]


class ModelProvider(ABC):
    """
    Abstract base class for LLM providers.

    Implementations must accept all configuration via __init__ and must
    never read from environment variables.
    """

    @abstractmethod
    def __init__(self, model: str, api_key: str, **kwargs: Any) -> None:
        pass

    @abstractmethod
    def generate(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> ProviderResponse:
        """
        Send a chat request to the model and return a normalized response.

        Args:
            messages: List of message dicts in OpenAI format
                      [{"role": "user"|"assistant"|"system"|"tool", "content": "..."}]
            tools: Optional list of tool definitions in JSON schema format
            temperature: Sampling temperature (0.0–2.0)
            max_tokens: Maximum tokens in the response
            timeout: Request timeout in seconds; None means no timeout

        Returns:
            ProviderResponse with content, tool_calls, and token counts
        """
        pass

    @abstractmethod
    def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Iterator[ProviderStreamEvent]:
        """
        Stream a chat response, yielding structured events.

        Args:
            messages: List of message dicts in OpenAI format
            tools: Optional list of tool definitions
            temperature: Sampling temperature
            max_tokens: Maximum tokens in the response
            timeout: Request timeout in seconds; None means no timeout

        Yields:
            TextDeltaEvent  {"type": "text_delta", "content": str}
                — zero or more partial text chunks as the model generates them

            StreamDoneEvent {"type": "done", "response": ProviderResponse}
                — exactly one final event with the complete ProviderResponse
                  (content, tool_calls, token counts, model name)
        """
        pass

    @abstractmethod
    def get_model_name(self) -> str:
        """Return the model identifier string."""
        pass

    @abstractmethod
    def supports_streaming(self) -> bool:
        """Return True if this provider supports streaming."""
        pass

    @abstractmethod
    def supports_tools(self) -> bool:
        """Return True if this provider supports tool calling."""
        pass
