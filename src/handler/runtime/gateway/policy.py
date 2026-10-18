"""Grant policy: exact action, then longest glob, then risk default, then ask."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

from ..context import RunContext
from ..types import Decision
from .base import ToolSpec

DEFAULT_RISK_DECISIONS = {
    "read": "allow",
    "write_internal": "allow",
    "write_external": "ask",
    "exec": "ask",
}


class GrantPolicy:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self._defaults = {**DEFAULT_RISK_DECISIONS, **(config.get("defaults") or {})}
        self._actions: dict[str, str] = dict(config.get("actions") or {})

    def decide(
        self, ctx: RunContext, spec: ToolSpec, args: dict[str, Any]
    ) -> tuple[Decision, str]:
        action = spec.action
        if action in self._actions:
            return Decision(self._actions[action]), f"action rule '{action}'"
        decision, reason = self._fallback(spec)
        if spec.explicit_allow_only and decision == Decision.ALLOW:
            return Decision.ASK, (f"{reason} would allow, but {action} needs an exact "
                                  "grant rule to run without approval")
        return decision, reason

    def _fallback(self, spec: ToolSpec) -> tuple[Decision, str]:
        action = spec.action
        globs = [p for p in self._actions if fnmatchcase(action, p)]
        if globs:
            best = max(globs, key=len)
            return Decision(self._actions[best]), f"action rule '{best}'"
        default = self._defaults.get(spec.risk.value)
        if default is not None:
            return Decision(default), f"default for {spec.risk.value}"
        return Decision.ASK, "no matching rule"
