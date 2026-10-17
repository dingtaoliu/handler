"""Run lifecycle: execute one claimed thread with its harness.

claim (dispatcher) → pending events → render one turn → harness session
→ stream events (bus + step recorder) → commit transcript, run, consumption.
Every write is fenced on the lease; a heartbeat keeps the lease alive and the
run is cancelled if the lease is lost.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .bus import BusMessage, RunEventBus
from .context import RunContext, reset_current_run, set_current_run
from .harness.base import Harness, HarnessError, RunEvent, RunEventKind, RunRequest
from .observability.pricing import PriceTable
from .observability.recorder import RawSink, StepRecorder
from .store.base import Store
from .types import (
    Event,
    Lease,
    Message,
    Role,
    Run,
    RunStatus,
    Thread,
    new_id,
    utcnow,
)

logger = logging.getLogger("handler.runtime.run")


@dataclass
class RunConfig:
    data_dir: Path
    lease_seconds: float = 120.0
    heartbeat_seconds: float = 30.0
    max_attempts: int = 3
    retry_backoff_seconds: tuple[float, ...] = (10.0, 60.0, 300.0)
    instructions: str = ""
    reasoning_effort: str | None = None
    timeout_seconds: float = 900.0
    harness_env: dict[str, str] = field(default_factory=dict)

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def homes_dir(self) -> Path:
        return self.data_dir / "harness_homes"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"


class LeaseLost(Exception):
    pass


def thread_slug(key: str) -> str:
    """Filesystem-safe, collision-resistant directory name for a thread key."""
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("_")[:60]
    digest = hashlib.sha256(key.encode()).hexdigest()[:10]
    return f"{readable}-{digest}"


def render_turn(events: list[Event]) -> str:
    """Render pending events as one turn for the harness.

    Chat messages become plain text; any other event is wrapped in an
    <event> tag so the agent can tell system events from user speech.
    """
    parts: list[str] = []
    for ev in events:
        if ev.type == "chat.message":
            parts.append(str(ev.payload.get("text", "")))
        else:
            body = json.dumps(ev.payload, ensure_ascii=False, default=str)
            parts.append(f'<event type="{ev.type}" source="{ev.source}">\n{body}\n</event>')
    return "\n\n".join(parts)


def event_messages(thread_key: str, run_id: str, events: list[Event]) -> list[Message]:
    msgs: list[Message] = []
    for ev in events:
        if ev.type == "chat.message":
            role, blocks = Role.USER, [{"type": "text", "text": ev.payload.get("text", "")}]
        else:
            role, blocks = Role.EVENT, [{"type": "event", "event_type": ev.type, "payload": ev.payload}]
        msgs.append(
            Message(thread_key=thread_key, role=role, blocks=blocks, run_id=run_id,
                    raw={"event_id": ev.id}, created_at=ev.received_at)
        )
    return msgs


def transcript_message(thread_key: str, run_id: str, ev: RunEvent) -> Message | None:
    """Convert a harness event into a transcript message (or None)."""
    if ev.kind is RunEventKind.ASSISTANT_TEXT:
        return Message(thread_key, Role.ASSISTANT, [{"type": "text", "text": ev.data.get("text", "")}],
                       run_id=run_id, raw=ev.raw)
    if ev.kind is RunEventKind.TOOL_FINISHED:
        d = ev.data
        blocks = [
            {"type": "tool_call", "id": d.get("id"), "name": d.get("tool"), "kind": d.get("kind"),
             "input": d.get("input")},
            {"type": "tool_result", "id": d.get("id"), "output": d.get("output"),
             "is_error": d.get("status") not in (None, "completed", "ok"),
             "exit_code": d.get("exit_code")},
        ]
        return Message(thread_key, Role.TOOL, blocks, run_id=run_id, raw=ev.raw)
    return None


class RunExecutor:
    def __init__(
        self,
        store: Store,
        harnesses: dict[str, Harness],
        bus: RunEventBus,
        config: RunConfig,
        prices: PriceTable,
    ) -> None:
        self.store = store
        self.harnesses = harnesses
        self.bus = bus
        self.config = config
        self.prices = prices
        self.raw_sink = RawSink(config.raw_dir)

    async def execute(self, lease: Lease) -> None:
        thread = await self.store.get_thread(lease.thread_key)
        events = await self.store.pending_events(lease.thread_key)
        if thread is None or not events:
            await self.store.complete(lease, run_id="", event_ids=[])
            return

        run = Run(
            id=new_id("run"),
            thread_key=thread.key,
            lease_version=lease.version,
            trigger_event_ids=[e.id for e in events],
            status=RunStatus.RUNNING,
            harness=thread.harness,
            model=thread.model,
        )
        if not await self.store.create_run(lease, run):
            logger.warning("lease lost before run start: %s", thread.key)
            return

        token = set_current_run(
            RunContext(run_id=run.id, thread_key=thread.key, user_id=thread.owner_user_id,
                       acting_as=thread.owner_user_id)
        )
        self._publish(thread.key, run.id, "run_started", {"trigger_event_ids": run.trigger_event_ids})
        logger.info("run started", extra={"harness": run.harness, "events": len(events)})
        try:
            await self._run_with_heartbeat(lease, thread, run, events)
        finally:
            reset_current_run(token)

    async def _run_with_heartbeat(
        self, lease: Lease, thread: Thread, run: Run, events: list[Event]
    ) -> None:
        work = asyncio.create_task(self._run(lease, thread, run, events))
        lost = asyncio.Event()

        async def heartbeat() -> None:
            current = lease
            while True:
                await asyncio.sleep(self.config.heartbeat_seconds)
                renewed = await self.store.heartbeat(current, self.config.lease_seconds)
                if renewed is None:
                    lost.set()
                    work.cancel()
                    return
                current = renewed

        hb = asyncio.create_task(heartbeat())
        try:
            await work
        except asyncio.CancelledError:
            if not lost.is_set():
                raise  # shutdown or user cancellation
            logger.warning("lease lost mid-run, abandoning: %s", thread.key)
            self._publish(thread.key, run.id, "run_finished", {"status": "abandoned"})
        finally:
            hb.cancel()

    async def _run(self, lease: Lease, thread: Thread, run: Run, events: list[Event]) -> None:
        harness = self.harnesses.get(thread.harness)
        recorder = StepRecorder(self.store, run.id, model=thread.model, prices=self.prices,
                                raw_sink=self.raw_sink)
        transcript = event_messages(thread.key, run.id, events)
        slug = thread_slug(thread.key)
        try:
            if harness is None:
                raise HarnessError(f"unknown harness {thread.harness!r}", retryable=False)
            workspace = self.config.workspaces_dir / slug
            home = self.config.homes_dir / slug
            workspace.mkdir(parents=True, exist_ok=True)
            home.mkdir(parents=True, exist_ok=True)
            req = RunRequest(
                run_id=run.id,
                thread_key=thread.key,
                workspace=workspace,
                home=home,
                prompt=render_turn(events),
                instructions=self.config.instructions,
                session_id=thread.session_id,
                model=thread.model,
                reasoning_effort=self.config.reasoning_effort,
                env=dict(self.config.harness_env),
            )
            final_text = ""
            async for ev in harness.run(req):
                await recorder.on_event(ev)
                self._publish(thread.key, run.id, "run_event",
                              {"kind": str(ev.kind), "data": _preview(ev.data)})
                if ev.kind is RunEventKind.SESSION:
                    sid = ev.data.get("session_id")
                    if sid and sid != thread.session_id:
                        if not await self.store.set_session(lease, sid):
                            raise LeaseLost(thread.key)
                        thread.session_id = sid
                elif ev.kind is RunEventKind.FINAL:
                    final_text = ev.data.get("text", "")
                msg = transcript_message(thread.key, run.id, ev)
                if msg is not None:
                    transcript.append(msg)

            usage, cost = recorder.totals()
            run.status, run.usage, run.cost_usd, run.finished_at = (
                RunStatus.COMPLETED, usage, cost, utcnow())
            ok = (
                await self.store.add_messages(lease, transcript)
                and await self.store.finish_run(lease, run)
                and await self.store.complete(lease, run.id, run.trigger_event_ids)
            )
            if not ok:
                raise LeaseLost(thread.key)
            logger.info("run completed", extra={"cost_usd": cost, "output_tokens": usage.output_tokens})
            self._publish(thread.key, run.id, "run_finished",
                          {"status": "completed", "text": final_text, "cost_usd": cost,
                           "usage": usage.__dict__})
        except asyncio.CancelledError:
            raise
        except LeaseLost:
            logger.warning("lease lost while committing run %s", run.id)
        except Exception as exc:  # harness errors and unexpected failures
            await self._fail(lease, thread, run, recorder, exc)

    async def _fail(self, lease: Lease, thread: Thread, run: Run, recorder: StepRecorder,
                    exc: Exception) -> None:
        retryable = isinstance(exc, HarnessError) and exc.retryable
        if not isinstance(exc, HarnessError):
            logger.exception("run crashed")
        attempt = thread.attempts + 1
        retry_in: float | None = None
        if retryable and attempt < self.config.max_attempts:
            backoff = self.config.retry_backoff_seconds
            retry_in = backoff[min(attempt - 1, len(backoff) - 1)]
        usage, cost = recorder.totals()
        run.status, run.usage, run.cost_usd = RunStatus.FAILED, usage, cost
        run.error, run.finished_at = f"{type(exc).__name__}: {exc}"[:2000], utcnow()
        await self.store.finish_run(lease, run)
        await self.store.fail(lease, run.error, retry_in)
        logger.warning("run failed", extra={"error": run.error, "retry_in": retry_in})
        self._publish(thread.key, run.id, "run_finished",
                      {"status": "failed", "error": run.error, "retry_in": retry_in})

    def _publish(self, thread_key: str, run_id: str | None, type_: str, data: dict) -> None:
        self.bus.publish(BusMessage(thread_key=thread_key, run_id=run_id, type=type_, data=data))


def _preview(data: dict, limit: int = 2000) -> dict:
    out = {}
    for k, v in data.items():
        if isinstance(v, str) and len(v) > limit:
            out[k] = v[:limit] + f"… [{len(v)} chars]"
        else:
            out[k] = v
    return out
