"""Harness adapter contract.

A harness is a vendor agent loop (Codex, Claude Code, ...). Handler never
implements the loop; an adapter starts or resumes the harness's session for a
thread, sends one turn, and streams normalized RunEvents back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

from ..types import TokenUsage, utcnow


class RunEventKind(StrEnum):
    SESSION = "session"  # data: {"session_id"}; emitted once the harness reports it
    TURN_STARTED = "turn_started"
    ASSISTANT_TEXT = "assistant_text"  # data: {"text"}
    REASONING = "reasoning"  # data: {"text"} (summary, may be empty)
    TOOL_STARTED = "tool_started"  # data: {"id", "tool", "kind", "input"}
    TOOL_FINISHED = "tool_finished"  # data: {"id", "tool", "kind", "input", "output", "status", "exit_code"?}
    # usage: TokenUsage for one turn or model call; data: {"model", "single_request"?}.
    # single_request=True means exactly one model request (enables per-request pricing).
    USAGE = "usage"
    ERROR = "error"  # data: {"message", "retryable": bool}
    FINAL = "final"  # data: {"text", "session_id"}; always the last event on success


@dataclass(frozen=True)
class RunEvent:
    kind: RunEventKind
    data: dict[str, Any] = field(default_factory=dict)
    usage: TokenUsage | None = None
    raw: dict[str, Any] | None = None  # harness-native event, kept verbatim
    at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class McpServerSpec:
    """An MCP server the harness should connect to (e.g. handler's gateway)."""

    name: str
    url: str | None = None  # streamable HTTP
    command: list[str] | None = None  # stdio
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RunLimits:
    timeout_seconds: float = 900.0
    max_turns: int | None = None


@dataclass(frozen=True)
class RunRequest:
    run_id: str
    thread_key: str
    workspace: Path  # working directory for the harness process
    home: Path  # isolated harness home (e.g. CODEX_HOME); holds session files
    prompt: str  # the rendered turn (new events only, never full history)
    instructions: str = ""  # system/developer instructions
    session_id: str | None = None  # resume this session if set
    model: str | None = None
    reasoning_effort: str | None = None
    mcp_servers: list[McpServerSpec] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)  # extra env for the process
    images: list[Path] = field(default_factory=list)  # image attachments for this turn
    limits: RunLimits = field(default_factory=RunLimits)
    # The thread's previous run never finished (worker died or lost its lease):
    # the harness should repair its session before sending this turn.
    repair_session: bool = False


class HarnessError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class Harness(Protocol):
    name: str

    def run(self, req: RunRequest) -> AsyncIterator[RunEvent]:
        """Run one turn. Yields events; the last event is FINAL on success.

        Raises HarnessError on failure (after yielding an ERROR event when
        possible). Must clean up the child process on cancellation.
        """
        ...

    async def interrupt(self, run_id: str) -> None:
        """Stop an in-flight run started with this run_id."""
        ...
