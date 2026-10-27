"""Questions: the agent asks people to pick an option, and continues on the answer.

`ask_user` parks a Question and notifies people; the run ends right away (runs
never wait for humans). Answering, expiring or canceling it emits one
`question.answered` event into the thread, which wakes the agent with a new
run. See docs/rebuild/ask-user.md.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from ..context import RunContext
from ..types import Event, Question, QuestionStatus, Risk, utcnow
from .base import ToolResult, ToolSpec
from .policy import UNTRUSTED_THREAD_TYPES

log = logging.getLogger(__name__)

ASKED_MESSAGE = ("Asked. End your turn; the answer will arrive in this thread "
                 "as a new message.")
MAX_QUESTION_CHARS = 2000
MAX_LABEL_CHARS = 80
MAX_TEXT_CHARS = 4000
MIN_OPTIONS, MAX_OPTIONS = 2, 5
NOTIFY_CHARS = 200


class QuestionNotPending(Exception):
    """The question was already answered, expired or canceled."""


class InvalidAnswer(ValueError):
    """The answer doesn't fit the question (unknown option, text not allowed...)."""


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def chat_owner(thread_key: str) -> str | None:
    """The user id in a `chat:{user}:{conversation}` key."""
    kind, _, rest = thread_key.partition(":")
    owner = rest.partition(":")[0] if kind == "chat" else ""
    return owner or None


def validate_ask(args: dict[str, Any]) -> tuple[str, list[str]]:
    """The cleaned question and option labels; ValueError on bad input."""
    question = str(args.get("question") or "").strip()
    if not question:
        raise ValueError("question must not be empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"question is too long (max {MAX_QUESTION_CHARS} characters)")
    options = args.get("options")
    if not isinstance(options, list) or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise ValueError(f"options must list {MIN_OPTIONS} to {MAX_OPTIONS} short labels")
    labels: list[str] = []
    for option in options:
        label = " ".join(str(option if isinstance(option, str) else "").split())
        if not label:
            raise ValueError("option labels must be non-empty strings")
        if len(label) > MAX_LABEL_CHARS:
            raise ValueError(f"option label is too long (max {MAX_LABEL_CHARS} "
                             f"characters): {label[:40]}…")
        if label.casefold() in {seen.casefold() for seen in labels}:
            raise ValueError(f"option labels must be unique: {label}")
        labels.append(label)
    return question, labels


class QuestionService:
    def __init__(
        self,
        store: Any,  # QuestionStore + get_thread
        *,
        emit_event: Callable[[Event], Awaitable[None]],
        notifier: Any = None,  # notifications.Notifier
        ttl_seconds: float = 86400,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._emit = emit_event
        self._notifier = notifier
        self._ttl = ttl_seconds
        self._clock = clock

    # ---- asking ------------------------------------------------------------

    async def ask(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        try:
            text, labels = validate_ask(args)
        except ValueError as exc:
            return ToolResult(f"Invalid question: {exc}", is_error=True)
        now = self._clock()
        question = Question(
            run_id=ctx.run_id, thread_key=ctx.thread_key,
            asked_by=ctx.acting_as or ctx.user_id, question=text,
            options=[{"id": f"o{i}", "label": label} for i, label in enumerate(labels, 1)],
            allow_text=bool(args.get("allow_text", False)),
            expires_at=now + timedelta(seconds=self._ttl),
            untrusted=ctx.thread_key.partition(":")[0] in UNTRUSTED_THREAD_TYPES,
            created_at=now,
        )
        await self._store.create_question(question)
        await self._notify(question, args.get("recipients"))
        return ToolResult({"status": "waiting_for_answer", "question_id": question.id,
                           "message": ASKED_MESSAGE})

    async def recipients_for(self, thread_key: str,
                             requested: list[str] | None) -> list[str] | str:
        """Chat: the thread's owner (only they can see it). Otherwise the
        requested user ids, or "all"."""
        if thread_key.startswith("chat:"):
            owner = None
            try:
                thread = await self._store.get_thread(thread_key)
                owner = thread.owner_user_id if thread is not None else None
            except Exception:
                log.warning("thread lookup for question recipients failed", exc_info=True)
            owner = owner or chat_owner(thread_key)
            return [owner] if owner else []
        wanted = [r.strip() for r in requested or [] if isinstance(r, str) and r.strip()]
        if not wanted or "all" in wanted:
            return "all"
        return list(dict.fromkeys(wanted))

    async def _notify(self, question: Question, requested: list[str] | None) -> None:
        if self._notifier is None:
            return
        try:
            recipients = await self.recipients_for(question.thread_key, requested)
            if not recipients:
                return
            message = f"Question: {_clip(question.question, NOTIFY_CHARS)}"
            await self._notifier.send(question.thread_key, question.run_id, message,
                                      recipients)
        except Exception:  # the question stands; people can still find it in the thread
            log.exception("notifying people about question %s failed", question.id)

    # ---- answering -----------------------------------------------------------

    async def answer(self, question_id: str, answered_by: str, *,
                     option_id: str | None = None, text: str | None = None) -> Question | None:
        """Record the first answer. None if no such question; raises
        QuestionNotPending (already resolved) or InvalidAnswer."""
        question = await self._store.get_question(question_id)
        if question is None:
            return None
        if question.status is not QuestionStatus.PENDING:
            raise QuestionNotPending(question.status.value)
        if question.expires_at and question.expires_at <= self._clock():
            await self._expire(question)
            raise QuestionNotPending(QuestionStatus.EXPIRED.value)
        answer = self._answer(question, option_id, text)
        moved = await self._store.transition_question(
            question_id, QuestionStatus.PENDING, QuestionStatus.ANSWERED,
            answer=answer, answered_by=answered_by)
        if not moved:
            raise QuestionNotPending("answered")
        resolved = await self._store.get_question(question_id) or question
        await self._resolved(resolved)
        return resolved

    @staticmethod
    def _answer(question: Question, option_id: str | None, text: str | None) -> dict[str, Any]:
        text = (text or "").strip() or None
        option_id = (option_id or "").strip() or None
        if text is not None and not question.allow_text:
            raise InvalidAnswer("this question takes one of its options, not free text")
        if text is not None and len(text) > MAX_TEXT_CHARS:
            raise InvalidAnswer(f"text is too long (max {MAX_TEXT_CHARS} characters)")
        if option_id is None and text is None:
            raise InvalidAnswer("pick an option" + (" or write an answer"
                                                    if question.allow_text else ""))
        label = None
        if option_id is not None:
            match = next((o for o in question.options if o.get("id") == option_id), None)
            if match is None:
                raise InvalidAnswer(f"unknown option: {option_id}")
            label = match.get("label")
        return {"option_id": option_id, "label": label, "text": text}

    async def cancel(self, question_id: str, canceled_by: str) -> Question | None:
        question = await self._store.get_question(question_id)
        if question is None:
            return None
        moved = await self._store.transition_question(
            question_id, QuestionStatus.PENDING, QuestionStatus.CANCELED)
        if not moved:
            raise QuestionNotPending(question.status.value)
        resolved = await self._store.get_question(question_id) or question
        await self._resolved(resolved, canceled_by=canceled_by)
        return resolved

    async def expire_stale(self) -> int:
        now = self._clock()
        pending = await self._store.list_questions(QuestionStatus.PENDING, limit=1000)
        count = 0
        for question in pending:
            if question.expires_at and question.expires_at <= now:
                count += await self._expire(question)
        return count

    async def _expire(self, question: Question) -> int:
        moved = await self._store.transition_question(
            question.id, QuestionStatus.PENDING, QuestionStatus.EXPIRED)
        if not moved:
            return 0
        resolved = await self._store.get_question(question.id) or question
        await self._resolved(resolved)
        return 1

    async def _display_name(self, user_id: str | None) -> str | None:
        if not user_id or not hasattr(self._store, "get_user"):
            return None
        try:
            user = await self._store.get_user(user_id)
        except Exception:
            return None
        return getattr(user, "display_name", None) or None

    async def _resolved(self, question: Question, *, canceled_by: str | None = None) -> None:
        who = question.answered_by or canceled_by
        event = Event(
            type="question.answered", source="gateway",
            payload={
                "question_id": question.id, "question": question.question,
                "status": question.status.value, "answer": question.answer,
                "answered_by": who, "answered_by_name": await self._display_name(who),
            },
            idempotency_key=f"question:{question.id}:answered",
            thread_key=question.thread_key, received_at=self._clock(),
        )
        try:
            await self._emit(event)
        except Exception:
            log.exception("emitting question.answered failed for %s", question.id)


ASK_USER_DESCRIPTION = (
    "Ask people a multiple-choice question when you are genuinely blocked on a decision "
    "a person must make. Give 2-5 short, concrete options. Returns right away: end your "
    "turn after asking; the answer arrives in this thread as a new message (or a note "
    "that the question expired after 24 hours). Don't ask when a reasonable default "
    "exists. On a chat, the person you're talking with is asked; elsewhere, "
    "`recipients` (user ids) or everyone."
)


class QuestionTools:
    def __init__(self, service: QuestionService) -> None:
        self.service = service

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(
            "ask_user", "question.ask", Risk.WRITE_INTERNAL, ASK_USER_DESCRIPTION,
            {"type": "object", "properties": {
                "question": {"type": "string", "minLength": 1,
                             "maxLength": MAX_QUESTION_CHARS},
                "options": {"type": "array", "minItems": MIN_OPTIONS,
                            "maxItems": MAX_OPTIONS,
                            "items": {"type": "string", "minLength": 1,
                                      "maxLength": MAX_LABEL_CHARS},
                            "description": "Short option labels; ids are generated."},
                "allow_text": {"type": "boolean", "default": False,
                               "description": "Also accept a typed answer."},
                "recipients": {"type": "array", "items": {"type": "string"},
                               "description": "Non-chat threads only: user ids to ask "
                                              "(default: everyone)."},
            }, "required": ["question", "options"], "additionalProperties": False},
            self.service.ask,
        )]
