"""Grant policy: exact action, then longest glob, then risk default, then ask.

Threads fed by outside input (email intake, Crontal notifications, Gmail,
connected IMAP mailboxes) get
a floor on top: their content is written by people outside the merchant, so
a call that sends the merchant's data outside it (publish, submit, message,
share, send mail...), runs code, or persists beyond the thread (memory,
automations) never runs there without approval, whatever the instance-wide
rules say (`crontal_api.*: allow` included). Writes that stay inside the
merchant (draft RFQs, draft bids, listings, contacts...) follow the normal
rules.
"""

from __future__ import annotations

import logging
from fnmatch import fnmatchcase
from typing import Any

from ..context import RunContext
from ..types import Decision, Risk
from .base import ToolSpec

log = logging.getLogger("handler.runtime.gateway.policy")

DEFAULT_RISK_DECISIONS = {
    "read": "allow",
    "write_internal": "allow",
    "write_external": "ask",
    "exec": "ask",
}

# Thread types (the thread key's prefix) whose turns carry outside input.
UNTRUSTED_THREAD_TYPES = frozenset({"email", "crontal", "gmail", "mail"})
# write_internal actions that stay inside the thread or the merchant, so they
# may still run unasked there. Every other write_internal (memory, automations,
# future tools) persists into what other threads read and needs approval.
INSIDE_ACTIONS = frozenset({
    "thread.set_title", "notify.send", "gmail.create_draft", "gmail.update_draft",
    "mail.draft_reply",
})


def thread_type_of(ctx: RunContext) -> str:
    return ctx.thread_key.partition(":")[0]


def floor_reason(spec: ToolSpec, args: dict[str, Any]) -> str | None:
    """Why `spec(args)` may not run unasked on an untrusted thread, else None."""
    if spec.outward is not None:
        try:
            outward = spec.outward(args)
        except Exception:  # a classifier bug must not let a call through
            log.exception("outward check for %s failed", spec.name)
            outward = "could not tell whether it leaves the merchant"
        if outward:
            return f"it {outward}"
        if spec.risk in (Risk.READ, Risk.WRITE_EXTERNAL):
            return None
    if spec.risk == Risk.EXEC:
        return "it runs code"
    if spec.risk == Risk.WRITE_EXTERNAL:
        return "it acts outside the merchant"
    if spec.risk == Risk.WRITE_INTERNAL and spec.action not in INSIDE_ACTIONS:
        return "it persists beyond this thread"
    return None


class GrantPolicy:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self._defaults = {**DEFAULT_RISK_DECISIONS, **(config.get("defaults") or {})}
        self._actions: dict[str, str] = dict(config.get("actions") or {})

    def decide(
        self, ctx: RunContext, spec: ToolSpec, args: dict[str, Any]
    ) -> tuple[Decision, str]:
        decision, reason = self._decide(spec)
        thread_type = thread_type_of(ctx)
        if decision == Decision.ALLOW and thread_type in UNTRUSTED_THREAD_TYPES:
            if why := floor_reason(spec, args):
                return Decision.ASK, (f"{reason} would allow, but this {thread_type} thread "
                                      f"carries outside input and {why}: approval needed")
        return decision, reason

    def _decide(self, spec: ToolSpec) -> tuple[Decision, str]:
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
