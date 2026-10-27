"""Question storage (ask_user) on both backends."""

import asyncio
from datetime import timedelta

from handler.runtime.store.postgres import PostgresStore
from handler.runtime.types import Question, QuestionStatus


def question(clock, offset: int = 0, **kwargs) -> Question:
    values = dict(run_id="r1", thread_key="crontal:rfq:1", asked_by="u1",
                  question="Which supplier?",
                  options=[{"id": "o1", "label": "Acme"}, {"id": "o2", "label": "Beta"}],
                  created_at=clock() + timedelta(seconds=offset),
                  expires_at=clock() + timedelta(days=1))
    values.update(kwargs)
    return Question(**values)


async def test_question_roundtrip_and_first_answer_wins(domain_store, clock):
    store = domain_store
    item = question(clock, allow_text=True, untrusted=True)
    await store.create_question(item)
    got = await store.get_question(item.id)
    assert got == item and item.id.startswith("q_")
    assert got.allow_text is True and got.untrusted is True
    assert got.status is QuestionStatus.PENDING and got.options[1]["label"] == "Beta"
    assert await store.get_question("missing") is None

    results = await asyncio.gather(*[
        store.transition_question(
            item.id, QuestionStatus.PENDING, QuestionStatus.ANSWERED,
            answer={"option_id": f"o{i % 2 + 1}", "label": "x", "text": None},
            answered_by=f"user{i}")
        for i in range(4)
    ])
    assert sum(results) == 1
    after = await store.get_question(item.id)
    assert after.status is QuestionStatus.ANSWERED and after.answered_at == clock()
    assert after.answered_by.startswith("user") and after.answer["label"] == "x"
    assert not await store.transition_question(
        item.id, QuestionStatus.PENDING, QuestionStatus.EXPIRED)


async def test_expire_without_answer_and_list_filters(domain_store, clock):
    store = domain_store
    first = question(clock, 0)
    second = question(clock, 1, run_id="r2")
    other = question(clock, 2, thread_key="chat:u2:c", allow_text=False)
    for item in (first, second, other):
        await store.create_question(item)
    assert await store.transition_question(first.id, QuestionStatus.PENDING,
                                           QuestionStatus.EXPIRED)
    expired = await store.get_question(first.id)
    assert expired.status is QuestionStatus.EXPIRED and expired.answer is None
    assert expired.answered_at is None and expired.allow_text is False

    assert [q.id for q in await store.list_questions()] == [other.id, second.id, first.id]
    assert [q.id for q in await store.list_questions(QuestionStatus.PENDING)] \
        == [other.id, second.id]
    assert [q.id for q in await store.list_questions(thread_key="chat:u2:c")] == [other.id]
    assert [q.id for q in await store.list_questions(
        QuestionStatus.PENDING, thread_key="crontal:rfq:1")] == [second.id]
    assert [q.id for q in await store.list_questions(run_ids=["r2"])] == [second.id]
    assert await store.list_questions(run_ids=[]) == []
    assert len(await store.list_questions(limit=1)) == 1


async def test_questions_table_is_created_per_instance_schema(pg_dsn, clock):
    stores = [PostgresStore(pg_dsn, clock=clock, schema=name) for name in ("inst_a", "inst_b")]
    try:
        item = question(clock)
        await stores[0].create_question(item)
        assert await stores[0].get_question(item.id) == item
        assert await stores[1].get_question(item.id) is None
        assert await stores[1].list_questions() == []
    finally:
        for store in stores:
            await store.close()
