"""Per-plan limits on what a tenant may spend: inputs per month and cost per run.

Inputs are counted per UTC calendar month. Plans come from HANDLER_PLAN_LIMITS and a
tenant's own `limits` overrides any key. See docs/rebuild/plan-limits.md.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

PLAN_LIMITS_ENV = "HANDLER_PLAN_LIMITS"
LIMIT_KEYS = frozenset({"monthly_inputs", "fix_model", "model", "max_run_tokens",
                        "max_run_turns", "connected_mail", "email", "automations", "at_limit", "allow_codex", "strict_grants",
                        "console", "max_upload_bytes", "max_workspace_bytes"})
_INT_KEYS = ("monthly_inputs", "max_run_tokens", "max_run_turns", "max_upload_bytes",
             "max_workspace_bytes")
DEFAULT_PLAN_TABLE: dict[str, dict[str, Any]] = {
    "paid": {},
    "free": {"monthly_inputs": 50, "at_limit": "drop", "fix_model": True, "model": "gpt-6.1-terra",
             "max_run_tokens": 200_000, "max_run_turns": 15, "connected_mail": False,
             "email": False, "automations": False, "allow_codex": False,
             "strict_grants": True, "console": False, "max_upload_bytes": 100 * 1024 * 1024,
             "max_workspace_bytes": 200 * 1024 * 1024},
}
# What happens to an event type at the limit. Anything not listed is internal
# (approval.resolved and the like) and is never counted.
HOLD, REFUSE, SKIP = "held", "refused", "skipped"
DROP = "over_limit"  # stored as a record only: never routed, at reset or after an upgrade
AT_LIMIT = ("hold", "drop")  # what a plan does with inputs that would be held
METERED = {"chat.message": REFUSE, "email.received": HOLD, "mail.received": HOLD,
           "crontal.notification": HOLD, "automation.fired": SKIP}


@dataclass(frozen=True)
class PlanLimits:
    plan: str
    monthly_inputs: int | None = None  # None: unlimited
    at_limit: str = "hold"  # "hold": release later; "drop": keep a record, never run
    fix_model: bool = False  # ignore thread, chat and settings model choices
    model: str | None = None  # the fixed model; None means the deployment's default
    max_run_tokens: int | None = None
    max_run_turns: int | None = None
    connected_mail: bool = True
    email: bool = True  # any inbound mail; off, it isn't stored at all
    automations: bool = True
    allow_codex: bool = True
    strict_grants: bool = False
    console: bool = True
    max_upload_bytes: int | None = None
    max_workspace_bytes: int | None = None


class LiveLimits:
    """Shared by a tenant's store and executor, so a plan change applies at once."""

    def __init__(self, current: PlanLimits) -> None:
        self.current = current


@dataclass(frozen=True)
class OverLimit:
    """`put`'s third outcome. Truthy when the input was stored (held, or kept as a
    record over the limit), so callers can still tell a new input from a duplicate."""

    action: str
    plan: str
    used: int
    limit: int
    resets_at: datetime
    notify: bool = False  # a skipped automation whose notice is due this period

    def __bool__(self) -> bool:
        return self.action in (HOLD, DROP)

    def payload(self) -> dict[str, Any]:
        """The stable body of a 429; Crontal's composer renders this."""
        return {"error": "input_limit_reached",
                "message": f"This workspace has used all {self.limit} messages included "
                           f"in its plan this month. They reset on "
                           f"{self.resets_at.date().isoformat()}.",
                "plan": self.plan, "usage": {"used": self.used, "limit": self.limit},
                "resets_at": self.resets_at.isoformat()}


def parse_plan_table(raw: str | None) -> dict[str, dict[str, Any]]:
    """The plan table: the defaults with HANDLER_PLAN_LIMITS laid over them."""
    table = {plan: dict(values) for plan, values in DEFAULT_PLAN_TABLE.items()}
    if raw:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"{PLAN_LIMITS_ENV} must be a JSON object of plans")
        for plan, values in parsed.items():
            check_limit_keys(values, f"{PLAN_LIMITS_ENV}[{plan}]", tenant=False)
            table.setdefault(plan, {}).update(values)
    return table


def check_limit_keys(values: Any, where: str, *, tenant: bool = True) -> None:
    if not isinstance(values, Mapping):
        raise ValueError(f"{where} must be an object")
    for key, value in values.items():
        if key == "max_runs":  # the run gate's per-tenant override; plans use HANDLER_PLAN_RUN_CAPS
            if not tenant:
                raise ValueError(f"{where}: set run caps per plan in HANDLER_PLAN_RUN_CAPS")
            continue
        if key not in LIMIT_KEYS:
            raise ValueError(f"{where}: unknown limit {key!r}")
        if key == "at_limit" and value not in AT_LIMIT:
            raise ValueError(f"{where}: at_limit must be one of {', '.join(AT_LIMIT)}")
        if key in _INT_KEYS and value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"{where}: {key} must be a non-negative integer or null")


def resolve_limits(plan: str, overrides: Mapping[str, Any],
                   table: Mapping[str, Mapping[str, Any]]) -> PlanLimits:
    """A tenant's limits. An unknown plan gets the free plan's, never unlimited."""
    values = {**table.get(plan, table["free"]), **{k: v for k, v in overrides.items()
                                                   if k in LIMIT_KEYS}}
    return PlanLimits(plan=plan, **values)


def upgrade_required(feature: str, limits: PlanLimits) -> dict[str, Any]:
    """The stable body of a 403 for a channel this plan doesn't include."""
    what = {"mailbox": "Connected mailboxes are", "gmail": "Gmail connections are",
            "console": "The assistant admin console is", "email": "Email is",
            "automations": "Automations are"}[feature]
    return {"error": "plan_requires_upgrade", "feature": feature, "plan": limits.plan,
            "message": f"{what} not included in the {limits.plan} plan. Upgrade to use it."}


def period_of(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def next_reset(now: datetime) -> datetime:
    current = now.astimezone(timezone.utc)
    year, month = (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)
