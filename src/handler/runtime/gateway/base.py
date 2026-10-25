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
# Before an `ask` call is parked: a human-readable summary for the approver and
# the arguments to store (e.g. with resolved recipients and pinned content
# hashes, so what runs after approval is exactly what was shown). Returning a
# ToolResult instead rejects the call with that (error) result.
ApprovalPreview = Callable[
    [RunContext, dict[str, Any]],
    Awaitable["tuple[str, dict[str, Any]] | ToolResult"],
]
# Whether one call sends the acting merchant's data outside it (publishes,
# submits, messages, shares, invites...): a short reason, or None when the
# call stays inside. See ToolSpec.outward.
OutwardCheck = Callable[[dict[str, Any]], "str | None"]


@dataclass(frozen=True)
class ToolSpec:
    """One MCP tool = one action.

    name: MCP tool name, snake_case, unique (e.g. "memory_save").
    action: dotted action id used by grants and audit (e.g. "memory.save").
    input_schema: JSON Schema (type: object) for the arguments.
    explicit_allow_only: only an exact grant rule for `action` may allow the
        call without approval; risk defaults and glob rules that say `allow`
        become `ask` (globs and defaults can still deny).
    approval_preview: see ApprovalPreview.
    outward: see OutwardCheck. On threads fed by outside input (email,
        crontal) an outward call always needs approval. A write_external or
        exec tool without it counts every call as outward.
    """

    name: str
    action: str
    risk: Risk
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler = field(compare=False, repr=False)
    explicit_allow_only: bool = False
    approval_preview: ApprovalPreview | None = field(default=None, compare=False, repr=False)
    outward: OutwardCheck | None = field(default=None, compare=False, repr=False)


class Policy(Protocol):
    def decide(self, ctx: RunContext, spec: ToolSpec, args: dict[str, Any]) -> tuple[Decision, str]:
        """Return the decision and a short human-readable reason."""
        ...


class ToolProvider(Protocol):
    """A module that contributes tools (memory, automation, notify, connectors...)."""

    def tools(self) -> list[ToolSpec]: ...
