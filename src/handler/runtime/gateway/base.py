"""Tool gateway contract.

Every tool handler exposes to a harness is a ToolSpec. The gateway serves
them over MCP, authorizes each call (grants → allow | ask | deny), audits it,
and parks `ask` calls as approvals that it executes itself once approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from ..context import RunContext
from ..types import Decision, Risk


@dataclass
class ToolResult:
    content: str | dict[str, Any] | list[Any]
    is_error: bool = False


ToolHandler = Callable[[RunContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    """One MCP tool = one action.

    name: MCP tool name, snake_case, unique (e.g. "memory_save").
    action: dotted action id used by grants and audit (e.g. "memory.save").
    input_schema: JSON Schema (type: object) for the arguments.
    """

    name: str
    action: str
    risk: Risk
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler = field(compare=False, repr=False)


class Policy(Protocol):
    def decide(self, ctx: RunContext, spec: ToolSpec, args: dict[str, Any]) -> tuple[Decision, str]:
        """Return the decision and a short human-readable reason."""
        ...


class ToolProvider(Protocol):
    """A module that contributes tools (memory, automation, notify, connectors...)."""

    def tools(self) -> list[ToolSpec]: ...
