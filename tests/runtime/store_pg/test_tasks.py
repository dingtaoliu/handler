"""Task queries return the same rows and derived statuses on SQLite and Postgres."""
from handler.runtime.tasks import get_task, list_tasks
from handler.runtime.types import (
    Approval,
    Event,
    Lane,
    Message,
    Question,
    QuestionStatus,
    Role,
    Run,
    RunStatus,
    ThreadSpec,
    ThreadType,
)


async def _start(store, clock, key, kind, payload, event_type="automation.fired"):
    lane = Lane.INTERACTIVE if kind is ThreadType.CHAT else Lane.BACKGROUND
    event = Event(type=event_type, source="test", payload=payload, idempotency_key=key,
                  thread_key=key, received_at=clock())
    await store.put(event, ThreadSpec(key, kind, lane, harness="codex"))
    lease, = await store.claim("worker", lane, 1, 60)
    run = Run(f"run_{key.replace(':', '_')}", key, lease.version, [event.id],
              RunStatus.RUNNING, "codex", None, started_at=clock())
    assert await store.create_run(lease, run)
    clock.advance(1)
    return lease, run


async def _complete(store, clock, lease, run, text):
    assert await store.add_messages(lease, [Message(run.thread_key, Role.ASSISTANT,
                                                    [{"type": "text", "text": text}],
                                                    run_id=run.id, created_at=clock())])
    run.status, run.finished_at = RunStatus.COMPLETED, clock()
    assert await store.finish_run(lease, run)
    assert await store.complete(lease, run.id, run.trigger_event_ids)


async def test_task_status_derivation_and_pagination(domain_store, clock):
    store = domain_store
    lease, done = await _start(store, clock, "automation:done", ThreadType.AUTOMATION, {"name": "Done"})
    await _complete(store, clock, lease, done, "All good")
    lease, dead = await _start(store, clock, "automation:dead", ThreadType.AUTOMATION, {"name": "Dead"})
    dead.status, dead.error, dead.finished_at = RunStatus.FAILED, "boom", clock()
    await store.finish_run(lease, dead)
    await store.fail(lease, "boom", None)
    lease, waiting = await _start(store, clock, "crontal:rfq:1", ThreadType.CRONTAL,
                                  {"type": "rfq_published", "entity_type": "rfq", "entity_id": 1},
                                  "crontal.notification")
    await _complete(store, clock, lease, waiting, "Need approval")
    await store.create_approval(Approval(run_id=waiting.id, thread_key=waiting.thread_key,
                                         tool="gmail_send", action="gmail.send", args={"to": "x"},
                                         requested_by=None, created_at=clock()))
    await _start(store, clock, "chat:u:1", ThreadType.CHAT, {"text": "hi"}, "chat.message")
    _, running = await _start(store, clock, "automation:live", ThreadType.AUTOMATION, {"name": "Live"})
    await store.put(Event(type="email.received", source="email", idempotency_key="mail",
                          payload={"subject": "Hi", "forwarder": {"address": "a@b.c"}},
                          received_at=clock()),
                    ThreadSpec("email:queued", ThreadType.EMAIL, Lane.BACKGROUND, harness="codex"))

    page = await list_tasks(store, user_id=None)
    statuses = {i["thread_key"]: i["status"] for i in page["items"]}
    assert statuses == {"email:queued": "queued", "automation:live": "running",
                        "crontal:rfq:1": "waiting_approval", "automation:dead": "dead",
                        "automation:done": "succeeded"}
    by_key = {i["thread_key"]: i for i in page["items"]}
    assert by_key["automation:done"]["summary"] == "All good"
    assert by_key["crontal:rfq:1"]["trigger_summary"] == "rfq 1 · rfq_published"
    assert by_key["email:queued"]["trigger_summary"] == "Email from a@b.c: Hi"
    assert by_key["crontal:rfq:1"]["pending_approval_ids"]

    first = await list_tasks(store, user_id=None, limit=2, statuses=["running", "waiting_approval",
                                                                       "succeeded", "dead"])
    second = await list_tasks(store, user_id=None, limit=2, cursor=first["next_cursor"])
    assert [i["run_id"] for i in first["items"]] == [running.id, waiting.id]
    assert [i["run_id"] for i in second["items"]] == [dead.id, done.id]
    filtered = await list_tasks(store, user_id=None, types=["crontal"])
    assert [i["thread_key"] for i in filtered["items"]] == ["crontal:rfq:1"]

    # The worker disappears: once its lease lapses the run is reported failed.
    clock.advance(120)
    item = await get_task(store, running.id, user_id=None, include_steps=True)
    assert item["status"] == "failed" and item["steps"] == []
    approval = (await get_task(store, waiting.id, user_id=None, include_steps=True))["steps"]
    assert [s["kind"] for s in approval] == ["approval"]


async def test_a_run_waiting_on_a_question_is_waiting_answer(domain_store, clock):
    store = domain_store
    lease, run = await _start(store, clock, "automation:ask", ThreadType.AUTOMATION,
                              {"name": "Ask"})
    await _complete(store, clock, lease, run, "Asked")
    question = Question(run_id=run.id, thread_key=run.thread_key, asked_by=None,
                        question="Which one?", options=[{"id": "o1", "label": "A"},
                                                        {"id": "o2", "label": "B"}],
                        created_at=clock())
    await store.create_question(question)
    (item,) = (await list_tasks(store, user_id=None))["items"]
    assert item["status"] == "waiting_answer"
    assert item["pending_question_ids"] == [question.id] and item["pending_approval_ids"] == []
    assert [i["run_id"] for i in (await list_tasks(
        store, user_id=None, statuses=["waiting_answer"]))["items"]] == [run.id]
    detail = await get_task(store, run.id, user_id=None, include_steps=True)
    (step,) = [s for s in detail["steps"] if s["kind"] == "question"]
    assert step["status"] == "pending" and step["data"]["question"] == "Which one?"

    assert await store.transition_question(
        question.id, QuestionStatus.PENDING, QuestionStatus.ANSWERED,
        answer={"option_id": "o1", "label": "A", "text": None}, answered_by="u")
    (item,) = (await list_tasks(store, user_id=None))["items"]
    assert item["status"] == "succeeded" and item["pending_question_ids"] == []
