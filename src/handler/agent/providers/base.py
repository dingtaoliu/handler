"""Model provider abstraction: base class and shared response types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ...event_store import EventStore


@dataclass
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    """Normalized response from a single LLM call."""

    text: str
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    # Provider-native response object — used by append_assistant_turn to reconstruct
    # the exact message format the API expects on the next turn.
    _native: Any = field(default=None, repr=False)

    @property
    def done(self) -> bool:
        return not self.tool_calls


class ModelProvider(ABC):
    """Adapts a specific LLM API to the protocol used by ManualAgent.

    Each provider handles:
    - Converting handler-internal messages to its own wire format
    - Making a single chat completion call
    - Appending the assistant turn and tool results to the running message list
    - Compacting old messages using a cheap model
    """

    @property
    @abstractmethod
    def compaction_model(self) -> str:
        """The cheap model used for summarization."""
        ...

    @abstractmethod
    def build_tool_defs(self, tools: list) -> tuple[list, dict[str, Any]]:
        """Convert handler tool objects to API-specific definitions.

        Returns (tool_defs, name→tool_object lookup).
        """
        ...

    @abstractmethod
    def format_messages(self, messages: list[dict]) -> list:
        """Convert handler internal messages to provider-native format."""
        ...

    @abstractmethod
    async def chat(
        self,
        system: str,
        messages: list,
        tool_defs: list,
    ) -> LLMResponse:
        """Make a single LLM call and return a normalized response."""
        ...

    @abstractmethod
    def append_assistant_turn(self, messages: list, response: LLMResponse) -> None:
        """Append the assistant message (with any tool calls) in provider format."""
        ...

    @abstractmethod
    def append_tool_results(
        self,
        messages: list,
        tool_calls: list[ToolCall],
        results: list[str],
    ) -> None:
        """Append tool result messages in provider format."""
        ...

    @abstractmethod
    async def compact(
        self,
        store: "EventStore",
        conversation_id: str,
        messages: list[dict],
        keep_recent: int,
    ) -> int:
        """Summarize old messages and persist. Returns number of messages compacted."""
        ...
