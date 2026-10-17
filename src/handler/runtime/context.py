"""Per-run context, carried in a ContextVar so concurrent runs never share state."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RunContext:
    run_id: str
    thread_key: str
    user_id: str | None = None  # None for system-triggered runs
    acting_as: str | None = None  # user whose authority the agent is exercising
    parent_run_id: str | None = None


_current: ContextVar[RunContext | None] = ContextVar("handler_run_context", default=None)


def current_run() -> RunContext | None:
    return _current.get()


def set_current_run(ctx: RunContext | None):
    """Set the run context; returns a token for ContextVar.reset()."""
    return _current.set(ctx)


def reset_current_run(token) -> None:
    _current.reset(token)
