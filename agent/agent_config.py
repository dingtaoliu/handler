"""
Agent configuration dataclass.

Pass an AgentConfig to AgentSession to control model selection, loop limits,
and the system prompt. All fields have sensible defaults and can be overridden
at construction time or via from_dict().
"""

from dataclasses import dataclass, field
from typing import Optional

DEFAULT_SYSTEM_PROMPT = "You are a helpful AI assistant."


@dataclass
class AgentConfig:
    """
    Immutable configuration for one agent request-response cycle.
    """

    # LLM settings
    model: str = "gpt-4o-mini"
    temperature: float = 1
    max_tokens: Optional[int] = None

    # Loop control
    max_iterations: int = 10
    llm_timeout: float = 90.0  # seconds per LLM call; 0 means no timeout

    # Feature flags
    enable_tools: bool = True
    enable_approval: bool = False  # require human confirmation for write tools

    # Prompt
    system_prompt: str = field(default_factory=lambda: DEFAULT_SYSTEM_PROMPT)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentConfig":
        """Construct from a dict, ignoring unknown keys."""
        allowed = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in allowed}
        return cls(**filtered)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "max_iterations": self.max_iterations,
            "enable_tools": self.enable_tools,
            "enable_approval": self.enable_approval,
            "llm_timeout": self.llm_timeout,
        }
