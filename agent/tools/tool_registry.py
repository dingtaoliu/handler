"""
ToolRegistry: catalog of available agent tools.

Register tools explicitly with registry.register(MyTool()).
The build_tool_registry() factory returns an empty registry — register
your application-specific tools there.
"""

import logging
from typing import Optional

from .base_tool import AgentTool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry of available agent tools."""

    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        """Register a tool instance."""
        if tool.name in self._tools:
            logger.warning(f"Tool '{tool.name}' is already registered — overwriting")
        self._tools[tool.name] = tool
        logger.debug(f"Registered tool: {tool.name}")

    def get(self, name: str) -> Optional[AgentTool]:
        """Return the tool with the given name, or None if not found."""
        return self._tools.get(name)

    def all(self) -> list[AgentTool]:
        """Return all registered tools."""
        return list(self._tools.values())

    def get_openai_schemas(self) -> list[dict]:
        """Return all tool schemas in OpenAI function-calling format."""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)


def build_tool_registry() -> ToolRegistry:
    """
    Return a ToolRegistry. Register your application-specific tools here.

    Example:
        from agent.tools.tool_registry import build_tool_registry
        from myapp.tools.my_tool import MyTool

        registry = build_tool_registry()
        registry.register(MyTool())
    """
    return ToolRegistry()
