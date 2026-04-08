"""Agent ABC: defines the interface that all agent backends must implement."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..context import AgentContext
from ..event_store import EventStore
from ..types import (
    RunContext,
    extract_text_content,
    image_path_to_base64_url,
    messages_to_openai,
    messages_to_openai_responses,
)

logger = logging.getLogger("handler.agent.base")

# Re-export so existing ``from .base import ...`` in sibling modules still works.
__all__ = [
    "BaseAgent",
    "extract_text_content",
    "image_path_to_base64_url",
    "messages_to_openai",
    "messages_to_openai_responses",
]


class BaseAgent(ABC):
    """Abstract base for agent backends (OpenAI, Claude, etc.).

    Subclasses own the inner reasoning loop — given a conversation's messages
    and system prompt they call the LLM, execute tool calls, and return the
    final response.  The outer event loop (routing events to conversations)
    lives in Environment.
    """

    def __init__(
        self,
        context: AgentContext,
        store: EventStore,
        run_ctx: RunContext,
        tools: list | None = None,
        model: str = "",
        max_turns: int = 20,
        compact_token_threshold: int = 100_000,
        keep_recent: int = 10,
    ):
        self.context = context
        self.store = store
        self.run_ctx = run_ctx
        self.model = model
        self.max_turns = max_turns
        self.compact_token_threshold = compact_token_threshold
        self.keep_recent = keep_recent
        self.tools = list(tools or [])

    @abstractmethod
    async def run(self, conversation_id: str, messages: list[dict]) -> str:
        """Run the agent on a conversation and return the final text response."""
        ...

    @abstractmethod
    async def end_session(self, conversation_id: str) -> None:
        """Persist important info to memory before the session is wiped."""
        ...

    @abstractmethod
    async def compact_conversation(self, conversation_id: str) -> int:
        """Compact older messages for a conversation and return the number compacted."""
        ...
