"""
AgentState and AgentStep dataclasses.

AgentState is the single source of truth for everything the agent knows
and has done within one event loop execution. It is created at the start
of each request and mutated in place as the loop progresses.

AgentStep captures everything that happened in one iteration of the loop,
forming the human-readable reasoning trace. Steps are flushed to agent_log
after the loop completes.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class AgentStep:
    """
    Record of one iteration of the agent loop.

    One step = one LLM call + zero or more tool executions.
    """

    iteration: int
    phase: str  # "planning" | "executing" | "complete" | "error"

    # LLM response for this step
    llm_content: Optional[str] = None  # text content from the LLM
    llm_reasoning: Optional[str] = None  # reasoning/thinking content (o1/o3 models)
    llm_model: Optional[str] = None  # model used
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None

    # Tool calls requested by the LLM in this step
    # Each entry: {"id": str, "name": str, "arguments": dict}
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    # Results of executing those tool calls
    # Each entry: {"id": str, "name": str, "result": Any, "error": str|None}
    tool_results: list[dict[str, Any]] = field(default_factory=list)

    # Timing
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @property
    def had_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def had_errors(self) -> bool:
        return any(r.get("error") for r in self.tool_results)


@dataclass
class AgentState:
    """
    Full state for one agent request-response cycle.

    Created fresh at the start of each request and mutated in place
    as the loop runs. Not persisted directly — only the final_response
    is written to the Message table; steps are flushed to agent_log.
    """

    # ── Input ──────────────────────────────────────────────────────────────
    user_message: str
    system_prompt: str

    # ── Conversation context ───────────────────────────────────────────────
    # Wire-format message list passed to the LLM on each call.
    # Starts as [system_prompt_msg, ...history...] and grows as the loop runs.
    messages: list[dict[str, Any]] = field(default_factory=list)

    # Condensed summary of history older than the context window (may be None)
    summary: Optional[str] = None

    # ── Loop control ───────────────────────────────────────────────────────
    iteration: int = 0
    max_iterations: int = 20
    phase: str = (
        "planning"  # "planning" | "executing" | "approval" | "complete" | "error"
    )

    # ── Reasoning trace ────────────────────────────────────────────────────
    # One AgentStep per loop iteration; flushed to agent_log after the loop.
    steps: list[AgentStep] = field(default_factory=list)

    # ── Human-in-the-loop ──────────────────────────────────────────────────
    needs_approval: bool = False

    # ── Output ────────────────────────────────────────────────────────────
    final_response: Optional[str] = None

    # ── Accounting ────────────────────────────────────────────────────────
    tokens_used: int = 0  # cumulative across all steps

    # ── Timing ─────────────────────────────────────────────────────────────
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # ── Convenience ───────────────────────────────────────────────────────
    @property
    def is_complete(self) -> bool:
        return self.phase in ("complete", "error")

    @property
    def elapsed_ms(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds() * 1000
        return None
