"""Cost allocation follows task origin, not whoever approves or owns a thread."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handler.runtime.observability.attribution import capture_attribution
from handler.runtime.observability.ledger import RunAttribution
from handler.runtime.types import Event


def event(kind, *, user=None, source="web", payload=None):
    return Event(kind, source, payload or {}, idempotency_key=kind, user_id=user)


@pytest.fixture
def store():
    return SimpleNamespace(
        get_approval=AsyncMock(return_value=None),
        get_question=AsyncMock(return_value=None),
        get_run_attribution=AsyncMock(return_value=None),
    )


def thread(owner="owner"):
    return SimpleNamespace(key="chat:shared", owner_user_id=owner)


async def capture(store, events, *, owner="owner", **kwargs):
    return await capture_attribution(store, thread(owner), SimpleNamespace(id="current"), events, **kwargs)


async def test_chat_initiator_gets_cost_even_when_thread_owner_differs(store):
    message = event("chat.message", user="visitor")
    result = await capture(store, [message], acting_as="configured", instance_name="bss")
    assert result.run_id == "current"
    assert result.thread_key == "chat:shared"
    assert result.instance_name == "bss"
    assert result.initiating_user_id == "visitor"
    assert result.attributed_user_id == "visitor"
    assert result.thread_owner_user_id == "owner"
    assert result.attribution_reason == "initiating_chat_user"
    assert result.trigger_event_ids == [message.id]
    assert result.trigger_type == "chat.message"


async def test_multiple_chat_users_stay_shared_even_with_background_actor(store):
    result = await capture(store, [event("chat.message", user="alice"), event("chat.message", user="bob")], acting_as="configured")
    assert result.initiating_user_id is None
    assert result.attributed_user_id is None
    assert result.attribution_reason == "multiple_chat_users_shared"


@pytest.mark.parametrize("owner,actor,attributed,reason", [
    ("owner", None, "owner", "thread_owner_background"),
    ("owner", "owner", "owner", "thread_owner_background"),
    ("owner", "configured", "configured", "configured_actor_background"),
    (None, "configured", "configured", "configured_actor_background"),
    (None, None, None, "shared_system"),
])
async def test_background_attribution_policy(store, owner, actor, attributed, reason):
    result = await capture(store, [event("automation.fired", source="scheduler")], owner=owner, acting_as=actor)
    assert result.initiating_user_id is None
    assert result.attributed_user_id == attributed
    assert result.attribution_reason == reason


async def test_approval_continuation_preserves_origin_but_records_approver(store):
    store.get_approval.return_value = SimpleNamespace(thread_key="chat:shared", run_id="original")
    original = RunAttribution("original", "chat:shared", attributed_user_id="requester")
    store.get_run_attribution.return_value = original
    result = await capture(store, [event("approval.resolved", source="gateway", payload={"approval_id": "approval", "decided_by": "approver"})], acting_as="configured")
    assert result.initiating_user_id == "approver"
    assert result.attributed_user_id == "requester"
    assert result.attribution_reason == "original_task_attribution"
    store.get_approval.assert_awaited_once_with("approval")
    store.get_run_attribution.assert_awaited_once_with("original")
    assert original.attributed_user_id == "requester"


async def test_shared_original_task_remains_shared_after_approval(store):
    store.get_approval.return_value = SimpleNamespace(thread_key="chat:shared", run_id="original")
    store.get_run_attribution.return_value = RunAttribution("original", "chat:shared", attributed_user_id=None)
    result = await capture(store, [event("approval.resolved", source="gateway", payload={"approval_id": "approval", "decided_by": 42})], acting_as="configured")
    assert result.initiating_user_id == "42"
    assert result.attributed_user_id is None
    assert result.attribution_reason == "original_task_attribution"


async def test_question_continuation_uses_same_origin_policy(store):
    store.get_question.return_value = SimpleNamespace(thread_key="chat:shared", run_id="original")
    store.get_run_attribution.return_value = RunAttribution("original", "chat:shared", attributed_user_id="requester")
    result = await capture(store, [event("question.answered", source="gateway", payload={"question_id": "question", "answered_by": "responder"})])
    assert result.initiating_user_id == "responder"
    assert result.attributed_user_id == "requester"
    store.get_question.assert_awaited_once_with("question")


async def test_untrusted_or_cross_thread_approval_does_not_change_task_attribution(store):
    store.get_approval.return_value = SimpleNamespace(thread_key="other", run_id="original")
    result = await capture(store, [event("approval.resolved", source="gateway", payload={"approval_id": "approval", "decided_by": "approver"})])
    assert result.attributed_user_id == "owner"
    store.get_run_attribution.assert_not_awaited()
    store.get_approval.reset_mock()
    result = await capture(store, [event("approval.resolved", source="email", payload={"approval_id": "approval", "decided_by": "sender"})])
    assert result.initiating_user_id is None
    assert result.attributed_user_id == "owner"
    store.get_approval.assert_not_awaited()
