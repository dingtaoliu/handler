"""
AgentLogger — writes AgentState steps to a JSON-lines log file after a loop run.

Usage:
    logger = AgentLogger(session_id="conv_42")
    logger.flush(state)

Each call to flush() appends one JSON object per AgentStep to the log file.
"""

import json
import logging
from datetime import date, datetime
from typing import Any, Optional

from .agent_state import AgentState, AgentStep

logger = logging.getLogger(__name__)

DEFAULT_LOG_FILE = "/tmp/agent.log"


def _json_safe(value: Any) -> Any:
    """Round-trip a value through JSON with date serialisation to make it safe."""
    if value is None:
        return None
    return json.loads(json.dumps(value, cls=_DateEncoder))


class _DateEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        return super().default(obj)


class AgentLogger:
    """
    Appends one JSON-lines entry per AgentStep to a log file.

    Args:
        session_id: Arbitrary identifier for this session (e.g. conversation ID).
        log_file:   Path to the log file. Defaults to /tmp/agent.log.
    """

    def __init__(
        self,
        session_id: str = "",
        log_file: Optional[str] = None,
    ) -> None:
        self.session_id = session_id
        self.log_file = log_file or DEFAULT_LOG_FILE

    def flush(self, state: AgentState) -> None:
        """
        Append all AgentStep records in *state* to the log file.

        Called once after AgentLoop.run() returns.
        """
        if not state.steps:
            return

        entries: list[dict] = []

        for step in state.steps:
            entries.append(self._step_to_entry(step))

        # If the loop ended in error, also write a top-level error entry.
        if state.phase == "error" and state.final_response:
            entries.append(
                {
                    "session_id": self.session_id,
                    "log_type": "error",
                    "error": state.final_response,
                    "started_at": state.started_at.isoformat() if state.started_at else None,
                    "completed_at": state.completed_at.isoformat() if state.completed_at else None,
                }
            )

        try:
            with open(self.log_file, "a") as f:
                for entry in entries:
                    f.write(json.dumps(entry, cls=_DateEncoder) + "\n")
            logger.debug(
                "Flushed %d agent log entries for session_id=%s",
                len(entries),
                self.session_id,
            )
        except Exception as exc:
            logger.warning("Failed to write agent log to %s: %s", self.log_file, exc)

    def _step_to_entry(self, step: AgentStep) -> dict:
        """Convert an AgentStep to a serializable log dict."""
        if step.phase == "error":
            log_type = "error"
        elif step.had_tool_calls:
            log_type = "tool_call"
        else:
            log_type = "llm_call"

        tool_names: Optional[str] = None
        if step.tool_calls:
            names = [tc.get("name", "") for tc in step.tool_calls]
            tool_names = ",".join(names) if names else None

        error: Optional[str] = None
        if step.had_errors:
            error_msgs = [r.get("error") for r in step.tool_results if r.get("error")]
            error = "; ".join(error_msgs)

        return {
            "session_id": self.session_id,
            "log_type": log_type,
            "iteration": step.iteration,
            "phase": step.phase,
            "model": step.llm_model,
            "tokens_in": step.tokens_in,
            "tokens_out": step.tokens_out,
            "tool_names": tool_names,
            "tool_calls": _json_safe(step.tool_calls),
            "tool_results": _json_safe(step.tool_results),
            "llm_content": step.llm_content,
            "error": error,
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
        }
