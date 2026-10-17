import asyncio
import hashlib
import json

from handler.runtime.context import RunContext
from handler.runtime.types import ApprovalStatus, Decision

CTX = RunContext(run_id="run1", thread_key="chat:u:1", user_id="u1", acting_as="u1")


async def test_allow_runs_handler_and_audits(gateway, store, tools):
    result = await gateway.call(CTX, "echo", {"text": "hi"})
    assert not result.is_error and result.content["echo"] == "hi"
    [entry] = await store.list_audit("run1")
    assert (entry.status, entry.decision, entry.action, entry.actor) == (
        "ok", Decision.ALLOW, "test.echo", "u1")
    canonical = json.dumps({"text": "hi"}, sort_keys=True, separators=(",", ":"))
    assert entry.args_digest == hashlib.sha256(canonical.encode()).hexdigest()
    assert entry.result_size > 0 and entry.duration_ms >= 0


async def test_args_preview_truncated(gateway, store):
    await gateway.call(CTX, "echo", {"text": "x" * 2000})
    [entry] = await store.list_audit()
    assert len(entry.args_preview) == 500


async def test_unknown_tool_and_validation_errors(gateway, store, tools):
    unknown = await gateway.call(CTX, "nope", {})
    assert unknown.is_error and "Unknown tool" in unknown.content
    missing = await gateway.call(CTX, "echo", {})
    assert missing.is_error and "text" in missing.content
    wrong = await gateway.call(CTX, "echo", {"text": 5})
    assert wrong.is_error
    extra = await gateway.call(CTX, "echo", {"text": "a", "z": 1})
    assert extra.is_error
    assert tools.calls == []


async def test_deny(gateway, store, tools):
    result = await gateway.call(CTX, "blocked", {"text": "a"})
    assert result.is_error and result.content.startswith("Denied: ")
    [entry] = await store.list_audit()
    assert (entry.status, entry.decision) == ("denied", Decision.DENY)
    assert tools.calls == []


async def test_ask_creates_pending_approval(gateway, store, tools):
    result = await gateway.call(CTX, "send_it", {"text": "mail"})
    assert not result.is_error
    assert result.content["status"] == "pending_approval"
    assert "Do not retry" in result.content["message"]
    approval = await store.get_approval(result.content["approval_id"])
    assert approval.status == ApprovalStatus.PENDING
    assert (approval.tool, approval.action, approval.args, approval.requested_by) == (
        "send_it", "test.send", {"text": "mail"}, "u1")
    assert approval.expires_at > approval.created_at
    [entry] = await store.list_audit()
    assert entry.status == "pending" and entry.approval_id == approval.id
    assert tools.calls == []


async def test_handler_exception_and_timeout(gateway, store):
    boom = await gateway.call(CTX, "boom", {"text": "a"})
    assert boom.is_error and "kaboom" in boom.content
    slow = await gateway.call(CTX, "slow", {"text": "a"})
    assert slow.is_error and "timed out" in slow.content
    statuses = [e.status for e in await store.list_audit()]
    assert statuses == ["error", "error"]


async def test_approve_executes_once_and_emits(gateway, store, tools, emitted):
    pending = (await gateway.call(CTX, "send_it", {"text": "mail"})).content
    approval = await gateway.approve(pending["approval_id"], "boss")
    assert approval.status == ApprovalStatus.EXECUTED
    assert approval.decided_by == "boss" and approval.decided_at is not None
    assert approval.result["content"]["echo"] == "mail"
    assert approval.result["content"]["as"] == "u1"
    assert tools.calls == [{"text": "mail"}]
    [event] = emitted
    assert event.type == "approval.resolved" and event.thread_key == "chat:u:1"
    assert event.idempotency_key == f"approval:{approval.id}:resolved"
    assert event.payload["status"] == "executed"
    assert event.payload["tool"] == "send_it" and event.payload["decided_by"] == "boss"
    assert await gateway.approve(approval.id, "boss") is None
    assert len(tools.calls) == 1
    statuses = [e.status for e in await store.list_audit("run1")]
    assert "ok" in statuses and "pending" in statuses


async def test_concurrent_approve_executes_once(gateway, tools):
    pending = (await gateway.call(CTX, "send_it", {"text": "mail"})).content
    results = await asyncio.gather(
        *[gateway.approve(pending["approval_id"], f"d{i}") for i in range(5)]
    )
    assert sum(r is not None for r in results) == 1
    assert len(tools.calls) == 1


async def test_failed_execution(gateway, store, emitted):
    approval = await store_approval(gateway, "boom_ext")
    done = await gateway.approve(approval.id, "boss")
    assert done.status == ApprovalStatus.FAILED and done.result["is_error"]
    assert emitted[-1].payload["status"] == "failed"


async def store_approval(gateway, _):
    """Create a pending approval for a tool that raises."""
    from handler.runtime.gateway import ToolResult, ToolSpec
    from handler.runtime.types import Risk

    async def bad(ctx, args):
        raise ValueError("nope")

    gateway.register(ToolSpec("boom_ext", "test.boomext", Risk.WRITE_EXTERNAL, "",
                              {"type": "object"}, handler=bad))
    result = await gateway.call(CTX, "boom_ext", {})
    return await gateway._store.get_approval(result.content["approval_id"])


async def test_deny_approval(gateway, store, tools, emitted):
    pending = (await gateway.call(CTX, "send_it", {"text": "m"})).content
    denied = await gateway.deny(pending["approval_id"], "boss", "no thanks")
    assert denied.status == ApprovalStatus.DENIED and denied.decided_by == "boss"
    assert await gateway.deny(pending["approval_id"], "boss") is None
    assert await gateway.approve(pending["approval_id"], "boss") is None
    assert tools.calls == []
    assert emitted[0].payload["status"] == "denied"
    assert "no thanks" in emitted[0].payload["result_preview"]
    assert len(emitted) == 1


async def test_expiry(gateway, store, tools, emitted, clock):
    pending = (await gateway.call(CTX, "send_it", {"text": "m"})).content
    other = (await gateway.call(CTX, "send_it", {"text": "n"})).content
    assert await gateway.expire_stale() == 0
    clock.advance(86400 + 1)
    assert await gateway.expire_stale() == 2
    assert await gateway.expire_stale() == 0
    assert (await store.get_approval(pending["approval_id"])).status == ApprovalStatus.EXPIRED
    assert {e.payload["status"] for e in emitted} == {"expired"}
    assert len(emitted) == 2
    assert await gateway.approve(other["approval_id"], "boss") is None
    assert tools.calls == []


async def test_approve_refuses_stale_pending(gateway, store, tools, clock, emitted):
    pending = (await gateway.call(CTX, "send_it", {"text": "m"})).content
    clock.advance(86400 + 1)
    assert await gateway.approve(pending["approval_id"], "boss") is None
    assert (await store.get_approval(pending["approval_id"])).status == ApprovalStatus.EXPIRED
    assert tools.calls == [] and len(emitted) == 1


async def test_duplicate_registration_rejected(gateway, tools):
    import pytest
    with pytest.raises(ValueError):
        gateway.register(tools)
