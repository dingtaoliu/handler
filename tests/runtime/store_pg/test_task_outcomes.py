"""Receipt-backed outcomes have identical SQLite/Postgres behavior."""
from types import SimpleNamespace

from handler.runtime.context import RunContext
from handler.runtime.task_outcomes import bind_run_task, capture_receipt, finish_task_run, outcome_view
from handler.runtime.tools.task_outcomes import TaskOutcomeTools
from handler.runtime.types import Approval, ApprovalStatus, Event, RunStatus


async def setup_task(store, run="run1", thread="chat:owner:one", event=None):
    run = SimpleNamespace(id=run, thread_key=thread, status=RunStatus.COMPLETED)
    await bind_run_task(store, run, [event or Event(type="chat.message", source="test", payload={}, idempotency_key="a")],
                        SimpleNamespace(key=thread))
    return run, RunContext(run.id, thread, user_id="owner", acting_as="owner")


async def report(store, ctx, outcome="completed", checks=None):
    return await TaskOutcomeTools(store)._report(ctx, {"outcome": outcome, "reason": "Checked source and receipt",
        "pending_work": [], "checks": checks or []})


async def test_completed_execution_does_not_imply_business_completion(domain_store):
    store = domain_store
    assert await outcome_view(store, "historical") is None
    run, ctx = await setup_task(store)
    await finish_task_run(store, run)
    assert (await outcome_view(store, run.id))["outcome"] == "awaiting_review"
    assert (await report(store, ctx)).is_error  # Words alone cannot establish success.
    receipt = await capture_receipt(store, ctx, "write_spreadsheet", {"path": "table.csv"}, "ok",
                                    {"rows": 102, "artifact": {"id": "artifact1"}, "body": "private"})
    bad = [{"receipt_id": receipt, "field": "rows", "expected": 16}]
    assert (await report(store, ctx, checks=bad)).is_error
    assert (await report(store, ctx, checks=[{"receipt_id": "other_task", "field": "rows", "expected": 102}])).is_error
    good = [{"receipt_id": receipt, "field": "rows", "expected": 102},
            {"receipt_id": receipt, "field": "artifact.id", "expected": "artifact1"}]
    assert not (await report(store, ctx, checks=good)).is_error
    assert (await outcome_view(store, run.id))["outcome"] == "completed"
    receipts = await store.task_receipts((await store.task_outcome_for_run(run.id))["id"])
    assert receipts[0]["facts"] == {"rows": 102, "artifact.id": "artifact1"}
    await capture_receipt(store, ctx, "crontal_api_get", {"path": "/api/v1/rfq/7"}, "ok", {"status": 200})
    assert (await outcome_view(store, run.id))["outcome"] == "awaiting_review"  # New work invalidates report.


async def test_errors_require_recovery_of_the_same_operation(domain_store):
    store = domain_store
    run, ctx = await setup_task(store)
    args = {"method": "POST", "path": "/api/v1/files/upload", "json": {"invalid": True}}
    error = await capture_receipt(store, ctx, "crontal_api_call", args, "error", "bad input")
    await capture_receipt(store, ctx, "crontal_api_get", {"path": "/api/v1/rfq"}, "ok", {"status": 200})
    view = await outcome_view(store, run.id)
    assert view["outcome"] == "partial" and view["unresolved_error_receipt_ids"] == [error]
    assert (await report(store, ctx)).is_error
    receipt = await capture_receipt(store, ctx, "crontal_api_call", {**args, "json": {"fixed": True}}, "ok",
                                    {"status": 201, "body": {"id": 7}})
    assert not (await report(store, ctx, checks=[{"receipt_id": receipt, "field": "body.id", "expected": 7}])).is_error
    assert (await outcome_view(store, run.id))["outcome"] == "completed"


async def test_rfq_job_progress_updates_original_task_without_claiming_coverage(domain_store):
    store = domain_store
    run, ctx = await setup_task(store)
    await capture_receipt(store, ctx, "crontal_api_call", {"method": "POST", "path": "/api/v1/rfq/received/from-document"},
                          "ok", {"status": 202, "body": {"id": 562, "intake_status": "extracting"}})
    await finish_task_run(store, run)
    assert (await outcome_view(store, run.id))["outcome"] == "in_progress"
    later, ctx2 = await setup_task(store, "later", "chat:owner:later")
    await capture_receipt(store, ctx2, "crontal_api_get", {"path": "/api/v1/rfq/received/562"}, "ok",
                          {"status": 200, "body": {"id": 562, "intake_status": "needs_review", "listings": [{}] * 16}})
    view = await outcome_view(store, run.id)
    assert view["outcome"] == "awaiting_review" and view["jobs"][0]["state"] == "needs_review"
    assert (await report(store, ctx, checks=[{"receipt_id": view["receipts"][0]["id"], "field": "body.id", "expected": 562}])).is_error
    receipt = await capture_receipt(store, ctx2, "crontal_api_get", {"path": "/api/v1/rfq/received/562"}, "ok",
                          {"status": 200, "body": {"id": 562, "intake_status": "confirmed", "listings": [{}] * 16}})
    assert (await outcome_view(store, run.id))["outcome"] == "awaiting_review"  # confirmed != source complete
    assert (await report(store, ctx2, checks=[{"receipt_id": receipt, "field": "body.listings.count", "expected": 102}])).is_error


async def test_pending_approval_and_resume_share_task(domain_store):
    store = domain_store
    run, ctx = await setup_task(store)
    approval = Approval(run_id=run.id, thread_key=ctx.thread_key, tool="mail_send", action="mail.send",
                        args={}, requested_by="owner", reason="send")
    await store.create_approval(approval)
    assert (await outcome_view(store, run.id))["outcome"] == "awaiting_approval"
    await store.transition_approval(approval.id, ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
    await store.transition_approval(approval.id, ApprovalStatus.APPROVED, ApprovalStatus.EXECUTED,
                                    result={"is_error": False, "content": {"status": "sent"}})
    event = Event(type="approval.resolved", source="gateway", payload={"approval_id": approval.id}, idempotency_key="resolved")
    resumed, ctx2 = await setup_task(store, "resumed", ctx.thread_key, event)
    task = await store.task_outcome_for_run(run.id)
    assert (await store.task_outcome_for_run(resumed.id))["id"] == task["id"]
    assert set(task["run_ids"]) == {run.id, resumed.id}
    receipt = await capture_receipt(store, ctx, "mail_send", {"draft_id": "draft1"}, "ok", {"status": "sent"})
    assert not (await report(store, ctx2, checks=[{"receipt_id": receipt, "field": "status", "expected": "sent"}])).is_error


async def test_real_gateway_approval_receipts_block_then_resolve(domain_store):
    from handler.runtime.gateway import Gateway, GrantPolicy
    from handler.runtime.gateway.base import ToolResult, ToolSpec
    from handler.runtime.settings import GrantsSettings
    from handler.runtime.types import Risk
    store = domain_store
    run, ctx = await setup_task(store)
    async def send(ctx, args):
        return ToolResult({"status": "sent", "message_id": "smtp-accepted"})
    async def emit(event):
        pass
    class Provider:
        def tools(self):
            return [ToolSpec("send_test", "test.send", Risk.WRITE_EXTERNAL, "test", {"type": "object"}, send)]
    gateway = Gateway(store, GrantPolicy(GrantsSettings().model_dump(mode="json")), emit_event=emit)
    gateway.register(Provider())
    waiting = await gateway.call(ctx, "send_test", {})
    assert waiting.content["handler_receipt_id"]
    assert (await outcome_view(store, run.id))["outcome"] == "awaiting_approval"
    approval = await gateway.approve(waiting.content["approval_id"], "owner")
    receipt = approval.result["content"]["handler_receipt_id"]
    assert not (await report(store, ctx, checks=[{"receipt_id": receipt, "field": "status", "expected": "sent"}])).is_error
    other, ctx2 = await setup_task(store, "other", "chat:owner:other")
    waiting = await gateway.call(ctx2, "send_test", {})
    await gateway.deny(waiting.content["approval_id"], "owner", "Do not send")
    assert (await outcome_view(store, other.id))["outcome"] == "blocked"


async def test_unknown_async_action_cannot_be_declared_complete(domain_store):
    store = domain_store
    run, ctx = await setup_task(store)
    await capture_receipt(store, ctx, "crontal_api_call", {"method": "POST", "path": "/api/v1/job"}, "ok",
                          {"status": 202, "body": {"job_id": "job1"}})
    view = await outcome_view(store, run.id)
    assert view["outcome"] == "awaiting_review" and view["pending_receipt_ids"]
    assert (await report(store, ctx)).is_error
