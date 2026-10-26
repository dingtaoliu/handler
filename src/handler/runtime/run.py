"""Run lifecycle: execute one claimed thread with its harness.

claim (dispatcher) → pending events → render one turn → harness session
→ stream events (bus + step recorder) → commit transcript, run, consumption.
Every write is fenced on the lease; a heartbeat keeps the lease alive and the
run is cancelled if the lease is lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .bus import BusMessage, RunEventBus
from .context import RunContext, current_run, reset_current_run, set_current_run
from .gateway.tokens import RunTokens
from .harness.base import (
    Harness,
    HarnessError,
    McpServerSpec,
    RunEvent,
    RunEventKind,
    RunLimits,
    RunRequest,
)
from .harness.codex import tool_input as codex_tool_input
from .images import MAX_IMAGE_BYTES, MAX_IMAGES_PER_MESSAGE, downscale, sniff_file
from .instructions import build_instructions
from .mail.intake import render_email_event, resolve_act_as
from .memory import MemoryTools
from .observability.pricing import PriceTable
from .observability.recorder import RawSink, StepRecorder
from .settings import SettingsService
from .tasks import publish_task
from .store.base import Store
from .uploads import UploadedFile, UploadStore
from .types import (
    Event,
    Lease,
    Message,
    Role,
    Run,
    RunStatus,
    StepKind,
    Thread,
    ThreadType,
    new_id,
    utcnow,
)
from .workspaces import WorkspaceSync

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


def chat_override(events: list[Event]) -> tuple[str | None, str | None]:
    """Model / reasoning effort picked on the newest chat message that sets one."""
    model = effort = None
    for ev in reversed(events):
        if ev.type != "chat.message":
            continue
        model = model or ev.payload.get("model") or None
        effort = effort or ev.payload.get("reasoning_effort") or None
        if model and effort:
            break
    return model, effort


# Turn text for integration-defined event types (e.g. "mail.received"); pure
# functions of the payload, so one process-wide table serves every instance.
EVENT_RENDERERS: dict[str, Callable[[dict, str], str]] = {}


def register_event_renderer(event_type: str, render: Callable[[dict, str], str]) -> None:
    EVENT_RENDERERS[event_type] = render


def render_turn(events: list[Event]) -> str:
    """Render pending events as one turn for the harness.

    Chat messages become plain text; any other event is wrapped in an
    <event> tag so the agent can tell system events from user speech.
    """
    parts: list[str] = []
    for ev in events:
        if ev.type == "chat.message":
            parts.append(str(ev.payload.get("text", "")))
        elif ev.type == "email.received":
            parts.append(render_email_event(ev.payload, ev.source))
        elif ev.type in EVENT_RENDERERS:
            parts.append(EVENT_RENDERERS[ev.type](ev.payload, ev.source))
        else:
            body = json.dumps(ev.payload, ensure_ascii=False, default=str)
            parts.append(f'<event type="{ev.type}" source="{ev.source}">\n{body}\n</event>')
    return "\n\n".join(parts)


def event_attachments(uploads: UploadStore, ev: Event) -> list[UploadedFile]:
    """The event's attached uploads that still exist and belong to its sender."""
    found = []
    for file_id in ev.payload.get("attachments") or []:
        meta = uploads.get(file_id) if isinstance(file_id, str) else None
        if meta is not None and meta.user_id == ev.user_id and uploads.path(meta) is not None:
            found.append(meta)
    return found


def event_messages(thread_key: str, run_id: str, events: list[Event],
                   uploads: UploadStore | None = None) -> list[Message]:
    msgs: list[Message] = []
    for ev in events:
        if ev.type == "chat.message":
            role, blocks = Role.USER, [{"type": "text", "text": ev.payload.get("text", "")}]
            if uploads is not None:
                blocks += [{"type": "file", "file_id": m.id, "filename": m.filename,
                            "content_type": m.content_type}
                           for m in event_attachments(uploads, ev)]
        else:
            role, blocks = Role.EVENT, [{"type": "event", "event_type": ev.type, "payload": ev.payload}]
        msgs.append(
            Message(thread_key=thread_key, role=role, blocks=blocks, run_id=run_id,
                    raw={"event_id": ev.id}, created_at=ev.received_at)
        )
    return msgs


def transcript_message(thread_key: str, run_id: str, ev: RunEvent,
                       tool_inputs: dict[str, Any] | None = None) -> Message | None:
    """Convert a harness event into a transcript message (or None).

    Tool calls become one message when they finish. Harnesses report a call's
    arguments on TOOL_STARTED, so pass the same `tool_inputs` dict for every
    event of a run: started inputs are remembered by call id and attached to
    the finished call (an input on the finished event itself wins).
    """
    if ev.kind is RunEventKind.TOOL_STARTED:
        if tool_inputs is not None and ev.data.get("id") is not None:
            tool_inputs[str(ev.data["id"])] = ev.data.get("input")
        return None
    if ev.kind is RunEventKind.ASSISTANT_TEXT:
        return Message(thread_key, Role.ASSISTANT, [{"type": "text", "text": ev.data.get("text", "")}],
                       run_id=run_id, raw=ev.raw)
    if ev.kind is RunEventKind.TOOL_FINISHED:
        d = ev.data
        started = (tool_inputs or {}).pop(str(d.get("id")), None)
        blocks = [
            {"type": "tool_call", "id": d.get("id"), "name": d.get("tool"), "kind": d.get("kind"),
             "input": d["input"] if d.get("input") is not None else started},
            {"type": "tool_result", "id": d.get("id"), "output": d.get("output"),
             "is_error": d.get("status") not in (None, "completed", "ok"),
             "exit_code": d.get("exit_code")},
        ]
        return Message(thread_key, Role.TOOL, blocks, run_id=run_id, raw=ev.raw)
    return None


def _step_input(preview: Any) -> Any:
    if not isinstance(preview, str):
        return preview
    try:
        return json.loads(preview)
    except ValueError:  # a plain string (e.g. a shell command) or a truncated preview
        return preview


async def fill_tool_inputs(store: Store, messages: list[Message]) -> list[Message]:
    """Fill in tool-call arguments missing from older transcript messages.

    Tool messages written before the call's arguments were carried onto the
    finished event have `input: None`. Codex messages keep the completed item
    (with its arguments) in `raw`; for other harnesses the run's TOOL_CALL
    step holds an input preview (truncated past the recorder's preview limit).
    """
    by_run: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.role != Role.TOOL:
            continue
        call = next((b for b in message.blocks if b.get("type") == "tool_call"), None)
        if call is None or call.get("input") is not None:
            continue
        item = (message.raw or {}).get("item")
        if isinstance(item, dict) and item.get("type"):
            call["input"] = codex_tool_input(item)
            continue
        if message.run_id is None or call.get("id") is None:
            continue
        if message.run_id not in by_run:
            by_run[message.run_id] = {
                str(step.data["id"]): step.data.get("input")
                for step in await store.list_steps(message.run_id)
                if step.kind == StepKind.TOOL_CALL and step.data.get("id") is not None
            }
        preview = by_run[message.run_id].get(str(call["id"]))
        if preview is not None:
            call["input"] = _step_input(preview)
    return messages


@dataclass
class RunPlan:
    instructions: str
    model: str | None
    reasoning_effort: str | None
    timeout_seconds: float
    max_attempts: int


class _RunRecorder(StepRecorder):
    async def session_retry(self, reason: str) -> None:
        event = RunEvent(RunEventKind.TURN_STARTED)
        await self.store.add_step(self._next(
            event, StepKind.RETRY, "session_recovery", ended_at=event.at,
            data={"reason": reason[:500]},
        ))


def _missing_session(error: HarnessError, session_id: str) -> bool:
    message = str(error).lower()
    identifier = re.escape(session_id.lower())
    session_end = (rf"(?: (?:with id |id )?['\"]?{identifier}['\"]?(?=$|[.\s:])"
                   r"|(?=$|[.\n:]))")
    return any(re.search(pattern, message) for pattern in (
        r"\bno (?:saved )?session found\b",
        rf"\b(?:session|conversation)(?: (?:with id |id )?['\"]?{identifier}['\"]?)? "
        r"(?:not found|does not exist|doesn't exist)\b",
        r"\b(?:could not|cannot|unable to) find (?:a |the )?(?:session|rollout for session)"
        + session_end,
        r"\bunknown session(?: id)?" + session_end,
        r"\bsession rollout files are missing\b",
    ))


def _has_rollout(home: Path, session_id: str) -> bool:
    return any(path.is_file() and path.name.endswith(f"-{session_id}.jsonl")
               for directory in ("sessions", "archived_sessions")
               for path in (home / directory).rglob("rollout-*.jsonl"))


class RunExecutor:
    def __init__(
        self,
        store: Store,
        harnesses: dict[str, Harness],
        bus: RunEventBus,
        config: RunConfig,
        prices: PriceTable,
        *,
        tokens: RunTokens | None = None,
        mcp_url: str | None = None,
        settings: SettingsService | None = None,
        memory_tools: MemoryTools | None = None,
        workspace_sync: WorkspaceSync | None = None,
        failure_notifier: Any = None,
    ) -> None:
        self.store = store
        # FailureNotifier (notifications.py): told about terminal background failures.
        self.failure_notifier = failure_notifier
        self.tokens = tokens
        self.mcp_url = mcp_url
        self.settings = settings
        self.memory_tools = memory_tools
        self.harnesses = harnesses
        self.bus = bus
        self.config = config
        self.prices = prices
        self.raw_sink = RawSink(config.raw_dir)
        self.uploads = UploadStore(config.data_dir / "uploads")
        self.workspace_sync = workspace_sync or WorkspaceSync(None, config.data_dir)

    async def execute(self, lease: Lease) -> None:
        thread = await self.store.get_thread(lease.thread_key)
        events = await self.store.pending_events(lease.thread_key)
        if thread is None or not events:
            await self.store.complete(lease, run_id="", event_ids=[])
            return

        # We hold the lease now, so any run of this thread still marked running
        # was left behind by a worker that died or lost its lease.
        await self.store.sweep_abandoned_runs(thread.key)
        previous = await self.store.list_runs(thread.key, limit=1)
        repair = bool(previous) and previous[0].status == RunStatus.ABANDONED
        run = Run(
            id=new_id("run"),
            thread_key=thread.key,
            lease_version=lease.version,
            trigger_event_ids=[e.id for e in events],
            status=RunStatus.RUNNING,
            harness=thread.harness,
            model=chat_override(events)[0] or thread.model,
        )
        if not await self.store.create_run(lease, run):
            logger.warning("lease lost before run start: %s", thread.key)
            return

        token = set_current_run(
            RunContext(run_id=run.id, thread_key=thread.key, user_id=thread.owner_user_id,
                       acting_as=await self._acting_as(thread))
        )
        self._publish(thread.key, run.id, "run_started", {"trigger_event_ids": run.trigger_event_ids})
        await self._publish_task(thread, run.id)
        logger.info("run started", extra={"harness": run.harness, "events": len(events)})
        try:
            await self._run_with_heartbeat(lease, thread, run, events, repair=repair)
        finally:
            reset_current_run(token)

    async def _renew(self, lease: Lease) -> Lease | None:
        """Extend the lease, retrying store errors while it is still valid."""
        delay = min(5.0, self.config.heartbeat_seconds)
        while True:
            try:
                return await self.store.heartbeat(lease, self.config.lease_seconds)
            except Exception:  # noqa: BLE001 - a DB blip must not kill the heartbeat
                left = (lease.until - utcnow()).total_seconds()
                logger.warning("heartbeat failed; retrying", exc_info=True,
                               extra={"lease_left_seconds": left})
                if left <= delay:
                    return None
                await asyncio.sleep(delay)

    async def _abandon(self, thread: Thread, run: Run, reason: str) -> None:
        """Stop the harness and mark the run row, best effort."""
        harness = self.harnesses.get(thread.harness)
        try:
            async with asyncio.timeout(5):
                if harness is not None:
                    await harness.interrupt(run.id)
                await self.store.abandon_run(run.id, reason)
        except Exception:  # noqa: BLE001 - the startup sweep catches what this misses
            logger.warning("could not mark run %s abandoned", run.id, exc_info=True)

    async def _run_with_heartbeat(
        self, lease: Lease, thread: Thread, run: Run, events: list[Event], *,
        repair: bool = False,
    ) -> None:
        work = asyncio.create_task(self._run(lease, thread, run, events, repair=repair))
        lost = asyncio.Event()

        async def heartbeat() -> None:
            current = lease
            while True:
                await asyncio.sleep(self.config.heartbeat_seconds)
                renewed = await self._renew(current)
                if renewed is None:
                    lost.set()
                    harness = self.harnesses.get(thread.harness)
                    if harness is not None:
                        # Stop the harness first: an SDK run left going would
                        # keep writing to the session after we hand it over.
                        with contextlib.suppress(Exception):
                            await harness.interrupt(run.id)
                    work.cancel()
                    return
                current = renewed

        hb = asyncio.create_task(heartbeat())
        try:
            await work
        except asyncio.CancelledError:
            if not lost.is_set():
                # Shutdown or user cancellation: the run cannot finish.
                await self._abandon(thread, run, "abandoned: the runtime stopped mid-run")
                raise
            logger.warning("lease lost mid-run, abandoning: %s", thread.key)
            await self._abandon(thread, run, "abandoned: lease lost mid-run")
            self._publish(thread.key, run.id, "run_finished", {"status": "abandoned"})
        finally:
            hb.cancel()

    async def _unsaved(self, thread_key: str, messages: list[Message]) -> list[Message]:
        """Drop event messages already saved by an earlier failed attempt."""
        if not messages:
            return messages
        # Saved ones sit at the tail: nothing else is saved while events are pending.
        recent = await self.store.list_messages(thread_key, limit=len(messages) + 20,
                                                newest=True)
        saved = {(m.raw or {}).get("event_id") for m in recent} - {None}
        return [m for m in messages if (m.raw or {}).get("event_id") not in saved]

    async def _run(self, lease: Lease, thread: Thread, run: Run, events: list[Event], *,
                   repair: bool = False) -> None:
        harness = self.harnesses.get(thread.harness)
        plan = await self._plan(thread, events)
        recorder = _RunRecorder(self.store, run.id, model=run.model, prices=self.prices,
                                raw_sink=self.raw_sink)
        transcript = await self._unsaved(
            thread.key, event_messages(thread.key, run.id, events, self.uploads))
        inputs = list(transcript)  # the turn's own messages, kept even if the run fails
        slug = thread_slug(thread.key)
        mcp_token: str | None = None
        try:
            if harness is None:
                raise HarnessError(f"unknown harness {thread.harness!r}", retryable=False)
            workspace = self.config.workspaces_dir / slug
            home = self.config.homes_dir / slug
            await self.workspace_sync.restore(slug)
            workspace.mkdir(parents=True, exist_ok=True)
            home.mkdir(parents=True, exist_ok=True)
            mcp_servers: list[McpServerSpec] = []
            if self.tokens is not None and self.mcp_url:
                current = current_run()
                mcp_token = self.tokens.issue(
                    RunContext(run_id=run.id, thread_key=thread.key,
                               user_id=thread.owner_user_id,
                               acting_as=current.acting_as if current is not None
                               and current.run_id == run.id else thread.owner_user_id),
                    ttl_seconds=plan.timeout_seconds + 300,
                )
                mcp_servers.append(McpServerSpec(
                    name="handler", url=self.mcp_url,
                    headers={"Authorization": f"Bearer {mcp_token}"},
                ))
            images, attached = self._attachments(events, workspace)
            prompt = render_turn(events)
            if attached:
                prompt += "\n\nAttached files: " + ", ".join(attached)
            req = RunRequest(
                run_id=run.id,
                thread_key=thread.key,
                workspace=workspace,
                home=home,
                prompt=prompt,
                images=images,
                instructions=plan.instructions,
                session_id=thread.session_id or None,
                model=plan.model,
                reasoning_effort=plan.reasoning_effort,
                mcp_servers=mcp_servers,
                env=dict(self.config.harness_env),
                limits=RunLimits(timeout_seconds=plan.timeout_seconds),
                repair_session=repair,
            )
            try:
                final_text = await self._stream(
                    lease, thread, run, harness, req, recorder, transcript,
                )
            except (asyncio.CancelledError, LeaseLost):
                # A stale owner must not overwrite the next owner's durable files.
                raise
            except Exception:
                self.workspace_sync.mark_dirty(slug)
                raise
            else:
                # Uploaded later (periodically and at shutdown), not on the
                # reply path: a per-run sync added minutes to every message.
                self.workspace_sync.mark_dirty(slug)

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
            await self._publish_task(thread, run.id)
        except asyncio.CancelledError:
            raise
        except LeaseLost:
            logger.warning("lease lost while committing run %s", run.id)
            await self._abandon(thread, run, "abandoned: lease lost mid-run")
        except Exception as exc:  # harness errors and unexpected failures
            await self._fail(lease, thread, run, recorder, exc, plan.max_attempts,
                             inputs=inputs)
        finally:
            if mcp_token is not None and self.tokens is not None:
                self.tokens.revoke(mcp_token)

    def _attachments(self, events: list[Event], workspace: Path) -> tuple[list[Path], list[str]]:
        """Copy every attachment under attachments/; images also go to the model.

        Vision images (PNG/JPEG/WebP/GIF by their bytes) are handed to the
        harness as image input, at most MAX_IMAGES_PER_MESSAGE per message;
        one over MAX_IMAGE_BYTES is sent as a downscaled copy. Returns the
        image paths (inside the workspace) and the prompt's file list.
        """
        images: list[Path] = []
        attached: list[str] = []
        for ev in events:
            shown = 0
            for meta in event_attachments(self.uploads, ev):
                source = self.uploads.path(meta)
                if source is None:
                    continue
                target_dir = workspace / "attachments"
                target_dir.mkdir(parents=True, exist_ok=True)
                name = meta.filename
                if (target_dir / name).exists():
                    name = f"{meta.id[:8]}-{name}"
                target = target_dir / name
                shutil.copyfile(source, target)
                rel = f"attachments/{name}"
                if sniff_file(target) is None:
                    attached.append(rel)
                    continue
                if shown >= MAX_IMAGES_PER_MESSAGE:
                    attached.append(f"{rel} (image not shown: over {MAX_IMAGES_PER_MESSAGE} "
                                    "per message)")
                    continue
                vision: Path | None = target
                if target.stat().st_size > MAX_IMAGE_BYTES:
                    vision = downscale(target, target_dir / f"{Path(name).stem}.downscaled",
                                       max_bytes=MAX_IMAGE_BYTES)
                if vision is None:
                    attached.append(f"{rel} (image too large to show)")
                    continue
                images.append(vision)
                shown += 1
                attached.append(f"{rel} (image, shown to you with this message)")
        return images, attached

    async def _stream(
        self, lease: Lease, thread: Thread, run: Run, harness: Harness,
        req: RunRequest, recorder: _RunRecorder, transcript: list[Message],
    ) -> str:
        input_count = len(transcript)
        # This turn's own messages may already be saved by a failed attempt.
        current = set(run.trigger_event_ids)
        if not req.session_id and await self.store.list_messages(thread.key, limit=1):
            # History without a harness session (e.g. imported from another
            # system): give the fresh session the thread's record as context.
            recap = await self._recap(
                thread.key, header="Earlier conversation in this thread (from handler's "
                "record; you have no session memory of it):", exclude_events=current)
            if recap:
                req = replace(req, prompt=recap + "\n\nCurrent turn:\n" + req.prompt)
        for attempt in range(2):
            final_text = ""
            progressed = False
            tool_inputs: dict[str, Any] = {}
            try:
                if (req.session_id and harness.name == "codex"
                        and not await asyncio.to_thread(_has_rollout, req.home, req.session_id)):
                    raise HarnessError("Session rollout files are missing from the harness home")
                async for ev in harness.run(req):
                    if ev.kind not in (RunEventKind.SESSION, RunEventKind.TURN_STARTED,
                                       RunEventKind.ERROR):
                        progressed = True
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
                    msg = transcript_message(thread.key, run.id, ev, tool_inputs)
                    if msg is not None:
                        transcript.append(msg)
                return final_text
            except HarnessError as exc:
                if (attempt or progressed or not req.session_id
                        or not _missing_session(exc, req.session_id)):
                    raise
                if not await self.store.set_session(lease, ""):
                    raise LeaseLost(thread.key)
                thread.session_id = None
                await recorder.session_retry(str(exc))
                recap = await self._recap(thread.key, exclude_events=current)
                req = replace(req, session_id=None, repair_session=False,
                              prompt=(recap + "\n\nCurrent turn:\n" if recap else "") + req.prompt)
                del transcript[input_count:]
        raise AssertionError("unreachable")

    async def _recap(
        self, thread_key: str,
        header: str = "Recent official transcript (context for the recovered session):",
        exclude_events: set[str] | None = None,
    ) -> str:
        """The last 20 messages as text, or "" when there are none.

        Messages of `exclude_events` (the current turn) are left out.
        """
        # Store pages start with the oldest messages, so grow the window until
        # it includes the tail rather than mistaking the first 20 for the last.
        limit = 200
        while True:
            messages = await self.store.list_messages(thread_key, limit=limit)
            if len(messages) < limit:
                break
            limit *= 2
        if exclude_events:
            messages = [m for m in messages
                        if (m.raw or {}).get("event_id") not in exclude_events]
        if not messages:
            return ""
        lines = [header]
        for message in messages[-20:]:
            content = json.dumps(message.blocks, ensure_ascii=False, default=str)
            excerpt = content if len(content) <= 500 else content[:500] + "…"
            lines.append(f"{message.role}: {excerpt}")
        return "\n".join(lines)

    async def _acting_as(self, thread: Thread) -> str | None:
        """Whose authority the run uses: the owner, or for email threads the
        live `email.act_as` setting (falling back to the owner set at ingest)."""
        if thread.type == ThreadType.EMAIL and self.settings is not None:
            email, _ = await self.settings.get("email")
            resolved = await resolve_act_as(self.store, email.get("act_as"))
            if resolved:
                return resolved
        return thread.owner_user_id

    async def _plan(self, thread: Thread, events: list[Event] | None = None) -> "RunPlan":
        """Resolve instructions, model and limits from settings (with config fallbacks)."""
        plan = RunPlan(
            instructions=self.config.instructions,
            model=thread.model,
            reasoning_effort=self.config.reasoning_effort,
            timeout_seconds=self.config.timeout_seconds,
            max_attempts=self.config.max_attempts,
        )
        override_model, override_effort = chat_override(events or [])
        if self.settings is None:
            plan.model = override_model or plan.model
            plan.reasoning_effort = override_effort or plan.reasoning_effort
            return plan
        threads, _ = await self.settings.get("threads")
        per_type = threads.get(str(thread.type), {}) or {}
        plan.model = thread.model or per_type.get("model")
        plan.reasoning_effort = per_type.get("reasoning_effort") or plan.reasoning_effort
        plan.model = override_model or plan.model
        plan.reasoning_effort = override_effort or plan.reasoning_effort
        limits, _ = await self.settings.get("limits")
        plan.timeout_seconds = float(limits.get("run_timeout_seconds", plan.timeout_seconds))
        plan.max_attempts = int(limits.get("max_attempts", plan.max_attempts))
        if self.memory_tools is not None:
            speaker = thread.owner_user_id if str(thread.type) == "chat" else None
            built = await build_instructions(self.settings, self.memory_tools,
                                             speaker=speaker, thread_type=thread.type)
            plan.instructions = "\n\n".join(p for p in (built, self.config.instructions) if p)
        return plan

    async def _fail(self, lease: Lease, thread: Thread, run: Run, recorder: StepRecorder,
                    exc: Exception, max_attempts: int, *,
                    inputs: list[Message] | None = None) -> None:
        retryable = isinstance(exc, HarnessError) and exc.retryable
        if not isinstance(exc, HarnessError):
            logger.exception("run crashed")
        attempt = thread.attempts + 1
        retry_in: float | None = None
        if retryable and attempt < max_attempts:
            backoff = self.config.retry_backoff_seconds
            retry_in = backoff[min(attempt - 1, len(backoff) - 1)]
        usage, cost = recorder.totals()
        run.status, run.usage, run.cost_usd = RunStatus.FAILED, usage, cost
        run.error, run.finished_at = f"{type(exc).__name__}: {exc}"[:2000], utcnow()
        await self.store.finish_run(lease, run)
        if inputs:
            # Keep what the user said in the record even though nobody answered;
            # a later attempt of the same events won't save it again (_unsaved).
            try:
                await self.store.add_messages(lease, inputs)
            except Exception:  # noqa: BLE001 - must not stop the thread being failed
                logger.exception("could not save the failed run's input messages")
        failed = await self.store.fail(lease, run.error, retry_in)
        logger.warning("run failed", extra={"error": run.error, "retry_in": retry_in})
        self._publish(thread.key, run.id, "run_finished",
                      {"status": "failed", "error": run.error, "retry_in": retry_in})
        await self._publish_task(thread, run.id)
        if failed and retry_in is None and self.failure_notifier is not None:
            try:
                await self.failure_notifier.run_failed(thread, run, await self._acting_as(thread))
            except Exception:
                logger.exception("failure notification failed")

    async def _publish_task(self, thread: Thread, run_id: str) -> None:
        """Live task status for background (non-chat) threads."""
        if thread.type != ThreadType.CHAT:
            await publish_task(self.bus, self.store, thread.key, run_id)

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
