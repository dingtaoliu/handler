"""The tool gateway: validate, authorize, execute, audit, and park approvals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from ..context import RunContext
from ..types import (
    Approval,
    ApprovalStatus,
    AuditEntry,
    Decision,
    Event,
    utcnow,
)
from .base import Policy, ToolProvider, ToolResult, ToolSpec

log = logging.getLogger(__name__)

PENDING_MESSAGE = (
    "This action needs a user's approval. Do not retry it; "
    "the decision will arrive in this thread."
)


def result_text(result: ToolResult) -> str:
    content = result.content
    return content if isinstance(content, str) else json.dumps(content, default=str)


def _digest(args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _preview(args: dict[str, Any], limit: int = 500) -> str:
    return json.dumps(args, sort_keys=True, default=str)[:limit]


def _validation_error(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    try:
        import jsonschema
    except ImportError:  # minimal fallback: required keys and top-level types
        return _basic_validation(schema, args)
    error = jsonschema.exceptions.best_match(
        jsonschema.Draft202012Validator(schema).iter_errors(args)
    )
    if error is None:
        return None
    where = ".".join(str(p) for p in error.absolute_path)
    return f"{where}: {error.message}" if where else error.message


def _basic_validation(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    types = {"string": str, "integer": int, "number": (int, float),
             "boolean": bool, "array": list, "object": dict}
    for key in schema.get("required", []):
        if key not in args:
            return f"'{key}' is a required property"
    for key, prop in (schema.get("properties") or {}).items():
        expected = types.get(prop.get("type", ""))
        if key in args and expected and not isinstance(args[key], expected):
            return f"{key}: {args[key]!r} is not of type '{prop['type']}'"
    return None


class Gateway:
    def __init__(
        self,
        store: Any,  # ApprovalStore + AuditStore
        policy: Policy,
        *,
        emit_event: Callable[[Event], Awaitable[None]],
        approval_ttl_seconds: float = 86400,
        call_timeout_seconds: float = 120,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._policy = policy
        self._emit = emit_event
        self._approval_ttl = approval_ttl_seconds
        self._timeout = call_timeout_seconds
        self._clock = clock
        self._tools: dict[str, ToolSpec] = {}

    def set_policy(self, policy: Policy) -> None:
        self._policy = policy

    def register(self, provider_or_spec: ToolProvider | ToolSpec) -> None:
        specs = (
            [provider_or_spec]
            if isinstance(provider_or_spec, ToolSpec)
            else provider_or_spec.tools()
        )
        for spec in specs:
            if spec.name in self._tools:
                raise ValueError(f"duplicate tool name: {spec.name}")
            self._tools[spec.name] = spec

    def decide(self, ctx: RunContext, spec: ToolSpec) -> tuple[Decision, str]:
        """The current policy decision for `spec` (with no arguments)."""
        return self._policy.decide(ctx, spec, {})

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    # ---- calls -------------------------------------------------------

    async def call(
        self, ctx: RunContext, tool_name: str, args: dict[str, Any] | None
    ) -> ToolResult:
        args = args or {}
        spec = self._tools.get(tool_name)
        if spec is None:
            return ToolResult(f"Unknown tool: {tool_name}", is_error=True)
        problem = _validation_error(spec.input_schema, args)
        if problem:
            return ToolResult(f"Invalid arguments for {tool_name}: {problem}", is_error=True)
        decision, reason = self._policy.decide(ctx, spec, args)
        actor = ctx.acting_as or ctx.user_id

        if decision == Decision.DENY:
            await self._audit(ctx, spec, args, Decision.DENY, "denied", error=reason)
            return ToolResult(f"Denied: {reason}", is_error=True)
        if decision == Decision.ASK:
            if spec.approval_preview is not None:
                try:
                    previewed = await spec.approval_preview(ctx, args)
                except Exception as exc:  # never raise to MCP
                    log.exception("approval preview for %s failed", spec.name)
                    previewed = ToolResult(f"Tool {spec.name} failed: {exc}", is_error=True)
                if isinstance(previewed, ToolResult):
                    await self._audit(ctx, spec, args, Decision.ASK, "error",
                                      error=result_text(previewed)[:500])
                    return previewed
                summary, args = previewed
                reason = f"{summary}\n\n({reason})"
            approval = Approval(
                run_id=ctx.run_id, thread_key=ctx.thread_key, tool=spec.name,
                action=spec.action, args=args, requested_by=actor, reason=reason,
                created_at=self._clock(),
                expires_at=self._clock() + timedelta(seconds=self._approval_ttl),
            )
            await self._store.create_approval(approval)
            await self._audit(ctx, spec, args, Decision.ASK, "pending",
                              approval_id=approval.id)
            return ToolResult({
                "status": "pending_approval", "approval_id": approval.id,
                "message": PENDING_MESSAGE,
            })
        result = await self._execute(ctx, spec, args, Decision.ALLOW)
        return result

    async def _execute(
        self, ctx: RunContext, spec: ToolSpec, args: dict[str, Any],
        decision: Decision, approval_id: str | None = None,
    ) -> ToolResult:
        started = asyncio.get_running_loop().time()
        error: str | None = None
        try:
            result = await asyncio.wait_for(spec.handler(ctx, args), self._timeout)
            if not isinstance(result, ToolResult):
                result = ToolResult(result)
            if result.is_error:
                error = result_text(result)[:500]
        except asyncio.TimeoutError:
            error = f"Tool {spec.name} timed out after {self._timeout:g}s"
            result = ToolResult(error, is_error=True)
        except Exception as exc:  # never raise to MCP
            log.exception("tool %s failed", spec.name)
            error = f"{type(exc).__name__}: {exc}"
            result = ToolResult(f"Tool {spec.name} failed: {error}", is_error=True)
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        await self._audit(
            ctx, spec, args, decision, "error" if error else "ok",
            result_size=len(result_text(result)), duration_ms=duration_ms,
            error=error, approval_id=approval_id,
        )
        return result

    async def _audit(
        self, ctx: RunContext, spec: ToolSpec, args: dict[str, Any],
        decision: Decision, status: str, *, result_size: int = 0,
        duration_ms: int = 0, error: str | None = None,
        approval_id: str | None = None,
    ) -> None:
        entry = AuditEntry(
            run_id=ctx.run_id, thread_key=ctx.thread_key,
            actor=ctx.acting_as or ctx.user_id, tool=spec.name, action=spec.action,
            decision=decision, status=status, args_digest=_digest(args),
            args_preview=_preview(args), result_size=result_size,
            duration_ms=duration_ms, error=error, approval_id=approval_id,
            at=self._clock(),
        )
        try:
            await self._store.add_audit(entry)
        except Exception:
            log.exception("audit write failed for %s", spec.name)

    # ---- approvals -----------------------------------------------------

    async def approve(self, approval_id: str, decided_by: str) -> Approval | None:
        approval = await self._store.get_approval(approval_id)
        if approval is None:
            return None
        if approval.expires_at and approval.expires_at <= self._clock():
            await self._expire(approval)
            return None
        moved = await self._store.transition_approval(
            approval_id, ApprovalStatus.PENDING, ApprovalStatus.APPROVED,
            decided_by=decided_by,
        )
        if not moved:
            return None
        spec = self._tools.get(approval.tool)
        ctx = RunContext(
            run_id=approval.run_id, thread_key=approval.thread_key,
            user_id=approval.requested_by, acting_as=approval.requested_by,
        )
        if spec is None:
            result = ToolResult(f"Unknown tool: {approval.tool}", is_error=True)
        else:
            result = await self._execute(ctx, spec, approval.args, Decision.ASK, approval.id)
        final = ApprovalStatus.FAILED if result.is_error else ApprovalStatus.EXECUTED
        stored = {"content": result.content, "is_error": result.is_error}
        await self._store.transition_approval(
            approval_id, ApprovalStatus.APPROVED, final, result=stored
        )
        resolved = await self._store.get_approval(approval_id)
        await self._resolved(resolved or approval, decided_by, result_text(result))
        return resolved

    async def deny(
        self, approval_id: str, decided_by: str, reason: str = ""
    ) -> Approval | None:
        approval = await self._store.get_approval(approval_id)
        if approval is None:
            return None
        moved = await self._store.transition_approval(
            approval_id, ApprovalStatus.PENDING, ApprovalStatus.DENIED,
            decided_by=decided_by,
            result={"content": reason or "Denied by user", "is_error": True},
        )
        if not moved:
            return None
        await self._audit_decision(approval, "denied", Decision.DENY, reason)
        resolved = await self._store.get_approval(approval_id)
        await self._resolved(resolved or approval, decided_by, reason)
        return resolved

    async def expire_stale(self) -> int:
        now = self._clock()
        pending = await self._store.list_approvals(ApprovalStatus.PENDING, limit=1000)
        count = 0
        for approval in pending:
            if approval.expires_at and approval.expires_at <= now:
                count += await self._expire(approval)
        return count

    async def _expire(self, approval: Approval) -> int:
        moved = await self._store.transition_approval(
            approval.id, ApprovalStatus.PENDING, ApprovalStatus.EXPIRED,
            result={"content": "Approval expired", "is_error": True},
        )
        if not moved:
            return 0
        await self._audit_decision(approval, "denied", Decision.DENY, "expired")
        resolved = await self._store.get_approval(approval.id)
        await self._resolved(resolved or approval, None, "expired")
        return 1

    async def _audit_decision(
        self, approval: Approval, status: str, decision: Decision, error: str
    ) -> None:
        try:
            await self._store.add_audit(AuditEntry(
                run_id=approval.run_id, thread_key=approval.thread_key,
                actor=approval.requested_by, tool=approval.tool,
                action=approval.action, decision=decision, status=status,
                args_digest=_digest(approval.args), args_preview=_preview(approval.args),
                error=error or None, approval_id=approval.id, at=self._clock(),
            ))
        except Exception:
            log.exception("audit write failed for approval %s", approval.id)

    async def _resolved(
        self, approval: Approval, decided_by: str | None, preview: str
    ) -> None:
        event = Event(
            type="approval.resolved", source="gateway",
            payload={
                "approval_id": approval.id, "tool": approval.tool,
                "action": approval.action, "status": approval.status.value,
                "decided_by": decided_by, "result_preview": preview[:500],
            },
            idempotency_key=f"approval:{approval.id}:resolved",
            thread_key=approval.thread_key, received_at=self._clock(),
        )
        try:
            await self._emit(event)
        except Exception:
            log.exception("emitting approval.resolved failed for %s", approval.id)
