"""ask_user: the tool, notifications, the answer API and expiry."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

from handler.runtime.auth import Authenticator
from handler.runtime.bus import RunEventBus
from handler.runtime.context import RunContext
from handler.runtime.gateway import (
    Gateway,
    GrantPolicy,
    QuestionService,
    QuestionTools,
    questions_router,
)
from handler.runtime.notifications import Notifier
from handler.runtime.sinks import NOTIFICATION_CREATED, Outbox, RetryPolicy
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.store.sqlite_users import User
from handler.runtime.types import Event, Lane, QuestionStatus, ThreadSpec, ThreadType

ADMIN = "test-admin-key"
CHAT = "chat:alice:c1"
CRONTAL = "crontal:rfq:7"


@dataclass
class Clock:
    now: datetime = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class FakeSink:
    name: str = "fake"
    retry: RetryPolicy = field(default_factory=lambda: RetryPolicy(3, (0.0,)))
    payloads: list = field(default_factory=list)

    def accepts(self, event) -> bool:
        return event.type == NOTIFICATION_CREATED

    async def deliver(self, payload):
        self.payloads.append(payload)


@dataclass
class World:
    store: SQLiteStore
    clock: Clock
    service: QuestionService
    gateway: Gateway
    outbox: Outbox
    sink: FakeSink
    emitted: list[Event]
    tokens: dict[str, str]


@pytest.fixture
async def world(tmp_path):
    clock = Clock()
    store = SQLiteStore(tmp_path / "h.db", clock=clock)
    tokens = {}
    for user_id, role in (("alice", "member"), ("bob", "member"), ("root", "admin")):
        await store.create_user(User(user_id, user_id.title(), role))
        _, tokens[user_id] = await store.create_token(user_id, "test")
    for key, kind, owner in ((CHAT, ThreadType.CHAT, "alice"),
                             (CRONTAL, ThreadType.CRONTAL, None)):
        await store.put(
            Event(type="seed", source="test", payload={}, idempotency_key=f"seed-{key}",
                  thread_key=key),
            ThreadSpec(key, kind, Lane.BACKGROUND, owner_user_id=owner, harness="fake"))
    emitted: list[Event] = []

    async def emit(event: Event) -> None:
        emitted.append(event)

    sink = FakeSink()
    outbox = Outbox(store, [sink])
    notifier = Notifier(store, RunEventBus(), outbox=outbox,
                        users_provider=lambda: ["alice", "bob"])
    service = QuestionService(store, emit_event=emit, notifier=notifier, clock=clock)
    gateway = Gateway(store, GrantPolicy(), emit_event=emit, clock=clock)
    gateway.register(QuestionTools(service))
    yield World(store, clock, service, gateway, outbox, sink, emitted, tokens)
    await store.close()


@pytest.fixture
async def client(world):
    app = FastAPI()
    app.include_router(questions_router(world.service, world.store,
                                        Authenticator(world.store, ADMIN)))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def as_(world: World, who: str) -> dict:
    token = ADMIN if who == "admin" else world.tokens[who]
    return {"Authorization": f"Bearer {token}"}


def ctx(thread_key: str, acting_as: str | None = "alice") -> RunContext:
    return RunContext(run_id="run_1", thread_key=thread_key, user_id=acting_as,
                      acting_as=acting_as)


async def ask(world: World, thread_key: str = CHAT, **args):
    args = {"question": "Which supplier should get the order?",
            "options": ["Acme", "Beta Steel"], **args}
    return await world.gateway.call(ctx(thread_key), "ask_user", args)


# ---- the tool ----------------------------------------------------------------

async def test_ask_creates_a_pending_question(world):
    result = await ask(world, allow_text=True)
    assert not result.is_error
    assert result.content["status"] == "waiting_for_answer"
    assert "End your turn" in result.content["message"]
    q = await world.store.get_question(result.content["question_id"])
    assert q.id.startswith("q_") and q.status is QuestionStatus.PENDING
    assert q.options == [{"id": "o1", "label": "Acme"}, {"id": "o2", "label": "Beta Steel"}]
    assert q.allow_text is True and q.asked_by == "alice" and q.run_id == "run_1"
    assert q.expires_at == world.clock() + timedelta(hours=24)
    assert q.untrusted is False
    assert world.emitted == []  # nothing wakes the agent until someone answers


@pytest.mark.parametrize("args, problem", [
    ({"options": ["only one"]}, "options"),
    ({"options": ["a", "b", "c", "d", "e", "f"]}, "options"),
    ({"options": ["Same", "same"]}, "unique"),
    ({"options": ["  ", "b"]}, "non-empty"),
    ({"options": ["x" * 81, "b"]}, "long"),
    ({"question": "x" * 2001}, "long"),
    ({"question": "   "}, "empty"),
])
async def test_ask_rejects_bad_input(world, args, problem):
    result = await ask(world, **args)
    assert result.is_error and problem in str(result.content)
    assert await world.store.list_questions() == []


async def test_chat_question_notifies_only_the_thread_owner(world):
    await ask(world, recipients=["bob"])  # ignored on a chat: only alice can see it
    assert [n.message for n in await world.store.list_notifications("alice")] \
        == ["Question: Which supplier should get the order?"]
    assert await world.store.list_notifications("bob") == []
    assert await world.outbox.deliver_once() == 1
    data = world.sink.payloads[0]["data"]
    assert data["recipients"] == ["alice"] and data["thread_key"] == CHAT


async def test_non_chat_question_notifies_recipients_or_everyone(world):
    await ask(world, CRONTAL, recipients=["bob"])
    assert len(await world.store.list_notifications("bob")) == 1
    assert await world.store.list_notifications("alice") == []
    await ask(world, CRONTAL)
    assert len(await world.store.list_notifications("alice")) == 1
    assert len(await world.store.list_notifications("bob")) == 2
    assert await world.outbox.deliver_once() == 2
    assert [p["data"]["recipients"] for p in world.sink.payloads] == [["bob"], "all"]


async def test_untrusted_threads_may_ask_without_approval(world):
    result = await ask(world, CRONTAL)
    assert result.content["status"] == "waiting_for_answer"  # not pending_approval
    q = await world.store.get_question(result.content["question_id"])
    assert q.untrusted is True


# ---- the API -----------------------------------------------------------------

async def test_answer_records_the_first_answer_and_wakes_the_agent(world, client):
    qid = (await ask(world)).content["question_id"]
    listed = (await client.get("/api/questions", params={"status": "pending"},
                               headers=as_(world, "alice"))).json()
    assert [q["id"] for q in listed] == [qid]
    assert (await client.get(f"/api/questions/{qid}", headers=as_(world, "alice"))) \
        .json()["options"][0] == {"id": "o1", "label": "Acme"}

    done = await client.post(f"/api/questions/{qid}/answer", json={"option_id": "o2"},
                             headers=as_(world, "alice"))
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "answered" and body["answered_by"] == "alice"
    assert body["answer"] == {"option_id": "o2", "label": "Beta Steel", "text": None}
    (event,) = world.emitted
    assert event.type == "question.answered" and event.thread_key == CHAT
    assert event.idempotency_key == f"question:{qid}:answered"
    assert event.payload["status"] == "answered"
    assert event.payload["answer"]["label"] == "Beta Steel"
    assert event.payload["answered_by"] == "alice"
    assert event.payload["answered_by_name"] == "Alice"

    again = await client.post(f"/api/questions/{qid}/answer", json={"option_id": "o1"},
                              headers=as_(world, "admin"))
    assert again.status_code == 409
    assert len(world.emitted) == 1


async def test_visibility_follows_the_thread(world, client):
    chat_q = (await ask(world)).content["question_id"]
    shared_q = (await ask(world, CRONTAL)).content["question_id"]
    # bob can't see alice's chat: 404 for reads and answers alike
    assert (await client.get(f"/api/questions/{chat_q}", headers=as_(world, "bob"))) \
        .status_code == 404
    assert (await client.post(f"/api/questions/{chat_q}/answer", json={"option_id": "o1"},
                              headers=as_(world, "bob"))).status_code == 404
    listed = (await client.get("/api/questions", headers=as_(world, "bob"))).json()
    assert [q["id"] for q in listed] == [shared_q]
    assert (await client.post("/api/questions/q_nope/answer", json={"option_id": "o1"},
                              headers=as_(world, "bob"))).status_code == 404
    # anyone can answer a question on a shared thread
    answered = await client.post(f"/api/questions/{shared_q}/answer",
                                 json={"option_id": "o1"}, headers=as_(world, "bob"))
    assert answered.status_code == 200 and answered.json()["answered_by"] == "bob"
    # admins see everything
    assert len((await client.get("/api/questions", headers=as_(world, "admin"))).json()) == 2


async def test_bad_answers_are_422(world, client):
    qid = (await ask(world)).content["question_id"]
    alice = as_(world, "alice")
    url = f"/api/questions/{qid}/answer"
    assert (await client.post(url, json={"option_id": "o9"}, headers=alice)).status_code == 422
    assert (await client.post(url, json={}, headers=alice)).status_code == 422
    # free text only when the question allows it
    assert (await client.post(url, json={"text": "Gamma"}, headers=alice)).status_code == 422
    assert (await client.post(url, json={"option_id": "o1", "text": "x"},
                              headers=alice)).status_code == 422
    assert (await client.get("/api/questions", params={"status": "bogus"},
                             headers=alice)).status_code == 422
    assert (await world.store.get_question(qid)).status is QuestionStatus.PENDING


async def test_text_answers_when_allowed(world, client):
    qid = (await ask(world, allow_text=True)).content["question_id"]
    url = f"/api/questions/{qid}/answer"
    done = await client.post(url, json={"text": " Gamma Metals "}, headers=as_(world, "alice"))
    assert done.status_code == 200
    assert done.json()["answer"] == {"option_id": None, "label": None, "text": "Gamma Metals"}

    both = (await ask(world, allow_text=True)).content["question_id"]
    done = await client.post(f"/api/questions/{both}/answer",
                             json={"option_id": "o1", "text": "but only 10t"},
                             headers=as_(world, "alice"))
    assert done.json()["answer"] == {"option_id": "o1", "label": "Acme", "text": "but only 10t"}


async def test_cancel_is_admin_only(world, client):
    qid = (await ask(world, CRONTAL)).content["question_id"]
    assert (await client.post(f"/api/questions/{qid}/cancel",
                              headers=as_(world, "bob"))).status_code == 403
    done = await client.post(f"/api/questions/{qid}/cancel", headers=as_(world, "admin"))
    assert done.status_code == 200 and done.json()["status"] == "canceled"
    assert world.emitted[-1].payload["status"] == "canceled"
    assert (await client.post(f"/api/questions/{qid}/cancel",
                              headers=as_(world, "admin"))).status_code == 409


# ---- expiry ------------------------------------------------------------------

async def test_expiry_is_recorded_and_wakes_the_agent(world, client):
    qid = (await ask(world)).content["question_id"]
    world.clock.advance(23 * 3600)
    assert await world.service.expire_stale() == 0
    world.clock.advance(3600)
    assert await world.service.expire_stale() == 1
    assert await world.service.expire_stale() == 0
    q = await world.store.get_question(qid)
    assert q.status is QuestionStatus.EXPIRED and q.answer is None
    (event,) = world.emitted
    assert event.payload["status"] == "expired" and event.payload["answered_by"] is None
    assert event.idempotency_key == f"question:{qid}:answered"
    late = await client.post(f"/api/questions/{qid}/answer", json={"option_id": "o1"},
                             headers=as_(world, "alice"))
    assert late.status_code == 409


async def test_answering_after_the_deadline_expires_it(world, client):
    qid = (await ask(world)).content["question_id"]
    world.clock.advance(24 * 3600 + 1)  # the sweep hasn't run yet
    late = await client.post(f"/api/questions/{qid}/answer", json={"option_id": "o1"},
                             headers=as_(world, "alice"))
    assert late.status_code == 409
    assert (await world.store.get_question(qid)).status is QuestionStatus.EXPIRED
    assert [e.payload["status"] for e in world.emitted] == ["expired"]
