"""Grant policy: exact action, then longest glob, then risk default, then ask.

Threads fed by outside input (email intake, Crontal notifications, Gmail,
connected IMAP mailboxes) get
a floor on top: their content is written by people outside the merchant, so
a call that sends the merchant's data outside it (publish, submit, message,
share, send mail...), runs code, or persists beyond the thread (memory
rewrites and deletes, automations) never runs there without approval, whatever the instance-wide
rules say (`crontal_api.*: allow` included). Writes that stay inside the
merchant (draft RFQs, draft bids, listings, contacts...) and append-only
memory saves (marked source_untrusted) follow the normal rules.

A second, per-call floor applies on every thread: a tool's `always_ask`
classifier flags calls that destroy data (deleting or trashing Crontal files,
emptying the trash), which never run unasked even under `crontal_api.*: allow`.
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
# question.ask only notifies the merchant's own users and waits for one of
# them to pick an answer (the record is flagged untrusted for the UI).
INSIDE_ACTIONS = frozenset({
    "thread.set_title", "notify.send", "question.ask", "gmail.create_draft", "gmail.update_draft",
    "mail.draft_reply", "mail.update_draft", "mail.remember", "approval.continuation", "task.report",
})
# Persisting actions the owner allows unasked on untrusted threads. memory.save
# only appends (nothing existing is lost), the agent is prompted to save only
# verified facts, and the topic is marked source_untrusted so every later
# prompt shows where it came from. memory.rewrite / memory.delete can destroy
# what other threads rely on, so they still ask (docs/rebuild/memory-kinds.md).
UNTRUSTED_ALLOWED_ACTIONS = frozenset({"memory.save"})


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
    if (spec.risk == Risk.WRITE_INTERNAL and spec.action not in INSIDE_ACTIONS
            and spec.action not in UNTRUSTED_ALLOWED_ACTIONS):
        return "it persists beyond this thread"
    return None


def always_ask_reason(spec: ToolSpec, args: dict[str, Any]) -> str | None:
    """Why `spec(args)` needs approval whatever the grants, else None."""
    if spec.always_ask is None:
        return None
    try:
        return spec.always_ask(args)
    except Exception:  # a classifier bug must not let a call through
        log.exception("always_ask check for %s failed", spec.name)
        return "could not tell whether it destroys data"


def strict_floor(ctx: RunContext, spec: ToolSpec, args: dict[str, Any],
                 decision: Decision, reason: str) -> tuple[Decision, str]:
    """The floor for plans with `strict_grants`; settings can only tighten it.

    Reads and writes kept inside Handler follow the normal rules. Code never runs.
    Calls into Crontal always need a person, and on threads fed by outside input,
    one that reaches outside the merchant or destroys data is refused outright.
    """
    if spec.risk == Risk.EXEC:
        return Decision.DENY, "running code is not included in this plan"
    if spec.risk != Risk.WRITE_EXTERNAL or decision == Decision.DENY:
        return decision, reason
    if thread_type_of(ctx) in UNTRUSTED_THREAD_TYPES:
        why = always_ask_reason(spec, args)
        if why is None and spec.outward is None:
            why = "acts outside the merchant"  # unclassified, so every call counts as outward
        elif why is None:
            try:
                why = spec.outward(args)
            except Exception:  # a classifier bug must not let a call through
                log.exception("outward check for %s failed", spec.name)
                why = "could not tell whether it leaves the merchant"
        if why:
            return Decision.DENY, f"on this plan a thread fed by outside input can't: it {why}"
    return Decision.ASK, f"{reason} would allow, but this plan needs approval"


class GrantPolicy:
    def __init__(self, config: dict[str, Any] | None = None, *, plan_limits: Any = None) -> None:
        config = config or {}
        self._plan_limits = plan_limits
        self._defaults = {**DEFAULT_RISK_DECISIONS, **(config.get("defaults") or {})}
        self._actions: dict[str, str] = dict(config.get("actions") or {})

    def decide(
        self, ctx: RunContext, spec: ToolSpec, args: dict[str, Any]
    ) -> tuple[Decision, str]:
        if why := self.excluded(spec):
            return Decision.DENY, why
        decision, reason = self._decide_with_floors(ctx, spec, args)
        if self._plan_limits is not None and self._plan_limits.current.strict_grants:
            return strict_floor(ctx, spec, args, decision, reason)
        return decision, reason

    def excluded(self, spec: ToolSpec) -> str | None:
        """Why the tenant's current plan leaves `spec` out entirely, else None.

        Read from the live limits, so the tool view built for each run (and every
        call) follows a plan change without a restart.
        """
        if self._plan_limits is None:
            return None
        limits = self._plan_limits.current
        if spec.action.startswith("automation.") and not limits.automations:
            return "automations are not included in this plan"
        if spec.risk == Risk.EXEC and limits.strict_grants:
            return "running code is not included in this plan"
        return None

    def _decide_with_floors(
        self, ctx: RunContext, spec: ToolSpec, args: dict[str, Any]
    ) -> tuple[Decision, str]:
        decision, reason = self._decide(spec)
        if decision == Decision.ALLOW and (why := always_ask_reason(spec, args)):
            return Decision.ASK, f"{reason} would allow, but it {why}: approval needed"
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
