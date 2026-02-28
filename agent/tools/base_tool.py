"""
AgentTool base class and ToolResult.

All agent tools must subclass AgentTool and implement execute().
Tools declare their own name, description, JSON schema, and whether
they require human approval before execution.
"""

import datetime
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


class _DateEncoder(json.JSONEncoder):
    """JSON encoder that serialises date/datetime objects to ISO strings."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, (datetime.date, datetime.datetime)):
            return obj.isoformat()
        return super().default(obj)


@dataclass
class ToolResult:
    """Outcome of executing a single tool call."""

    tool_name: str
    success: bool
    result: Any = None  # serializable result returned to the LLM
    error: Optional[str] = None  # error message if success is False

    def to_message(self) -> dict:
        """Format as an OpenAI-style tool result message."""
        if self.success:
            content = (
                json.dumps(self.result, cls=_DateEncoder)
                if not isinstance(self.result, str)
                else self.result
            )
        else:
            content = f"Error: {self.error}"
        return {"role": "tool", "content": content}


class AgentTool(ABC):
    """
    Abstract base class for all agent tools.

    Subclasses must define:
        name            (str)  — unique identifier exposed to the LLM
        description     (str)  — used in the LLM prompt
        parameters      (dict) — JSON schema for input arguments
        requires_approval (bool) — True for write operations

    And implement:
        execute(session, **kwargs) -> ToolResult
    """

    name: str
    description: str
    parameters: dict  # JSON schema
    requires_approval: bool = False

    @abstractmethod
    def execute(self, session: Any, **kwargs: Any) -> ToolResult:
        """
        Execute the tool.

        Args:
            session: AgentSession providing user_id, auth_token, workspace, etc.
            **kwargs: Tool-specific arguments matching the parameters schema

        Returns:
            ToolResult
        """
        pass

    def to_openai_schema(self) -> dict:
        """Return the OpenAI function-calling tool schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
