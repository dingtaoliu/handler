"""Model provider abstraction: base class and shared response types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from ...types import extract_text_content

if TYPE_CHECKING:
    from ...event_store import EventStore


COMPACTION_SYSTEM = (
    "You are a conversation summarizer. Produce a concise but complete summary that preserves:\n"
    "- All facts, decisions, and conclusions reached\n"
    "- Key numbers, dates, names, and values mentioned\n"
    "- Any tasks or follow-ups\n"
    "- The user's goals and context\n\n"
    "If a prior summary is provided, incorporate it so the output covers the full conversation history.\n"
    "Be dense and factual. This summary replaces the original messages in the context window."
)


def build_compaction_prompt(
    store: "EventStore",
    conversation_id: str,
    messages: list[dict],
    keep_recent: int,
) -> tuple[list[dict], str]:
    """Slice messages and build the summarization prompt.

    Returns (to_compact, user_content) where to_compact is the messages being
    summarized and user_content is the formatted string to send to the LLM.
    Returns an empty list and empty string if there is nothing to compact.
    """
    to_compact = messages[:-keep_recent]
    if not to_compact:
        return [], ""
    existing_summary = store.get_latest_summary(conversation_id)
    parts: list[str] = []
    if existing_summary:
        parts.append(f"## Prior Summary\n{existing_summary}")
    parts.append("## Conversation")
    for m in to_compact:
        parts.append(f"{m['role'].capitalize()}: {extract_text_content(m['content'])}")
    return to_compact, "\n\n".join(parts)


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
