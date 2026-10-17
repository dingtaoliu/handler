from handler.runtime.types import Approval, ApprovalStatus, AuditEntry, Decision


def approval(**kw):
    return Approval(run_id="r", thread_key="t", tool="x", action="a.x",
                    args={"k": [1, {"z": None}]}, requested_by="u", **kw)


async def test_approval_roundtrip_and_cas(store):
    a = approval()
    await store.create_approval(a)
    got = await store.get_approval(a.id)
    assert got.args == a.args and got.status == ApprovalStatus.PENDING
    assert got.created_at == a.created_at
    assert await store.get_approval("missing") is None
    assert not await store.transition_approval(
        a.id, ApprovalStatus.APPROVED, ApprovalStatus.EXECUTED)
    assert await store.transition_approval(
        a.id, ApprovalStatus.PENDING, ApprovalStatus.APPROVED, decided_by="boss")
    got = await store.get_approval(a.id)
    assert got.decided_by == "boss" and got.decided_at is not None
    assert await store.transition_approval(
        a.id, ApprovalStatus.APPROVED, ApprovalStatus.EXECUTED,
        result={"content": {"a": 1}, "is_error": False})
    got = await store.get_approval(a.id)
    assert got.result == {"content": {"a": 1}, "is_error": False}
    assert got.decided_by == "boss"  # untouched when no decider given


async def test_list_approvals_filter_and_order(store):
    first, second = approval(), approval()
    await store.create_approval(first)
    await store.create_approval(second)
    await store.transition_approval(first.id, ApprovalStatus.PENDING, ApprovalStatus.DENIED,
                                    decided_by="b")
    assert [a.id for a in await store.list_approvals(ApprovalStatus.PENDING)] == [second.id]
    assert len(await store.list_approvals()) == 2
    assert len(await store.list_approvals(limit=1)) == 1


async def test_audit_roundtrip_newest_first(store):
    for i, run in enumerate(["r1", "r2", "r1"]):
        await store.add_audit(AuditEntry(
            run_id=run, thread_key="t", actor=None, tool=f"t{i}", action="a.b",
            decision=Decision.ALLOW, status="ok", error=None))
    assert [e.tool for e in await store.list_audit()] == ["t2", "t1", "t0"]
    entries = await store.list_audit("r1")
    assert [e.tool for e in entries] == ["t2", "t0"]
    assert entries[0].decision == Decision.ALLOW and entries[0].actor is None
