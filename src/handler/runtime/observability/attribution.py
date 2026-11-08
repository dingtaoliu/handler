"""Snapshot attribution once; a thread owner is not necessarily the initiator."""

from .ledger import RunAttribution


async def capture_attribution(store, thread, run, events, *, instance_name="main", acting_as=None):
    chat_users = {e.user_id for e in events if e.type == "chat.message" and e.user_id}
    initiating = set(chat_users)
    origin_runs = set()
    for event in events:
        if event.source != "gateway":
            continue
        if event.type == "approval.resolved":
            if who := event.payload.get("decided_by"):
                initiating.add(str(who))
            approval = await store.get_approval(event.payload.get("approval_id", ""))
            if approval and approval.thread_key == thread.key:
                origin_runs.add(approval.run_id)
        elif event.type == "question.answered":
            if who := event.payload.get("answered_by"):
                initiating.add(str(who))
            question = await store.get_question(event.payload.get("question_id", ""))
            if question and question.thread_key == thread.key:
                origin_runs.add(question.run_id)
    attributed = None
    reason = "shared_system"
    if len(chat_users) == 1:
        attributed, = chat_users
        reason = "initiating_chat_user"
    elif len(chat_users) > 1:
        reason = "multiple_chat_users_shared"
    elif origin_runs:
        originals = [await store.get_run_attribution(rid) for rid in sorted(origin_runs)]
        known = [a for a in originals if a is not None]
        users = {a.attributed_user_id for a in known}
        if len(known) == len(origin_runs) and len(users) == 1:
            attributed, = users
            reason = "original_task_attribution"
        elif known:
            reason = "mixed_or_incomplete_original_attribution"
        else:
            attributed = acting_as or thread.owner_user_id
            reason = "legacy_origin_thread_owner" if attributed else "shared_system"
    elif acting_as or thread.owner_user_id:
        attributed = acting_as or thread.owner_user_id
        reason = "configured_actor_background" if acting_as and acting_as != thread.owner_user_id else "thread_owner_background"
    return RunAttribution(
        run_id=run.id, thread_key=thread.key, instance_name=instance_name or "main",
        initiating_user_id=next(iter(initiating)) if len(initiating) == 1 else None,
        attributed_user_id=attributed, thread_owner_user_id=thread.owner_user_id,
        attribution_reason=reason,
        trigger_type=",".join(sorted({e.type for e in events})),
        trigger_event_ids=[e.id for e in events])
