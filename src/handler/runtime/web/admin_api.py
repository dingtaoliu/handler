"""Memory, automations, usage, tools, uploads, logs and connections endpoints."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from ..auth import Authenticator, Principal, can_see_thread
from ..context import RunContext
from ..gateway.gateway import Gateway
from ..memory import MemoryTools
from ..scheduler import Scheduler, parse_spec
from ..settings import resolve_harness
from ..store.base import Store
from ..types import Event, Lane, MemoryTopic, Run, Schedule, ThreadSpec, ThreadType, TokenUsage
from ..uploads import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_REQUEST,
    UploadStore,
    UploadTooLarge,
)

THREAD_SCAN_LIMIT = 10000
RUN_SCAN_LIMIT = 5000
LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30, "ERROR": 40, "CRITICAL": 50}
LOG_TAIL_MAX_LINES = 5000


class MemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str
    description: str = ""


class AutomationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    prompt: str
    schedule: str
    timezone: str = "UTC"
    notify: list[str] = []


class AutomationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    prompt: str | None = None
    schedule: str | None = None
    timezone: str | None = None
    notify: list[str] | None = None
    enabled: bool | None = None


def _topic_summary(topic: MemoryTopic) -> dict[str, Any]:
    return {"name": topic.name, "description": topic.description, "version": topic.version,
            "updated_by": topic.updated_by, "updated_at": jsonable_encoder(topic.updated_at),
            "size": len(topic.content.encode())}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _tail(path: Path, max_lines: int) -> list[str]:
    """The last `max_lines` lines of a file, reading backwards in blocks."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            end = f.tell()
            data = b""
            while end > 0 and data.count(b"\n") <= max_lines:
                start = max(0, end - 65536)
                f.seek(start)
                data = f.read(end - start) + data
                end = start
    except FileNotFoundError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


def aggregate_usage(runs: list[Run], thread_of: dict[str, str], days: int) -> dict[str, Any]:
    """Totals and breakdowns by day, model and thread for already filtered runs."""

    def bucket() -> dict[str, Any]:
        return {"usage": TokenUsage(), "cost": 0.0, "priced": 0, "runs": 0}

    def add(b: dict[str, Any], run: Run) -> None:
        b["usage"] = b["usage"] + run.usage
        b["runs"] += 1
        if run.cost_usd is not None:
            b["cost"] += run.cost_usd
            b["priced"] += 1

    def render(b: dict[str, Any], **extra: Any) -> dict[str, Any]:
        u: TokenUsage = b["usage"]
        return {**extra, "input_tokens": u.input_tokens, "cached_input_tokens": u.cached_input_tokens,
                "output_tokens": u.output_tokens, "reasoning_tokens": u.reasoning_tokens,
                "cost_usd": b["cost"] if b["priced"] else None, "runs": b["runs"]}

    total, by_day, by_model, by_thread = bucket(), defaultdict(bucket), defaultdict(bucket), defaultdict(bucket)
    for run in runs:
        add(total, run)
        add(by_day[run.started_at.astimezone(timezone.utc).date().isoformat()], run)
        add(by_model[run.model or "unknown"], run)
        add(by_thread[run.thread_key], run)
    top = sorted(by_thread.items(),
                 key=lambda kv: (kv[1]["usage"].input_tokens + kv[1]["usage"].output_tokens),
                 reverse=True)[:50]
    return {
        "days": days,
        "totals": render(total),
        "by_day": [render(b, day=d) for d, b in sorted(by_day.items())],
        "by_model": [render(b, model=m) for m, b in sorted(
            by_model.items(), key=lambda kv: kv[1]["usage"].output_tokens, reverse=True)],
        "by_thread": [render(b, thread_key=k, type=thread_of.get(k)) for k, b in top],
    }


def admin_router(
    store: Store, auth: Authenticator, *, scheduler: Scheduler, gateway: Gateway,
    uploads: UploadStore, log_file: Path | None = None,
) -> APIRouter:
    router = APIRouter()
    user = Depends(auth.require_principal)
    admin = Depends(auth.require_admin)

    # ---- memory ----------------------------------------------------------

    def topic_name(name: str) -> str:
        try:
            return MemoryTools._name(name)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @router.get("/api/memory")
    async def list_memory(_: Principal = user) -> list:
        return [_topic_summary(t) for t in await store.list_topics()]

    @router.get("/api/memory/{name:path}")
    async def get_memory(name: str, _: Principal = user) -> dict:
        topic = await store.get_topic(topic_name(name))
        if topic is None:
            raise HTTPException(404, "Topic not found")
        return {**_topic_summary(topic), "content": topic.content}

    @router.put("/api/memory/{name:path}")
    async def put_memory(name: str, body: MemoryBody, if_match: str | None = Header(None),
                         principal: Principal = user) -> dict:
        name = topic_name(name)
        try:
            expected = int(if_match.strip().strip('"')) if if_match is not None else None
        except ValueError:
            raise HTTPException(422, "If-Match must contain an integer version")
        for _attempt in range(3):
            current = await store.get_topic(name)
            if expected is None:
                version = current.version if current else None  # last write wins
            else:
                version = expected
            topic = MemoryTopic(name, body.description, body.content, updated_by=principal.identity)
            stored = await store.put_topic(topic, version)
            if stored is not None:
                return {**_topic_summary(stored), "content": stored.content}
            if expected is not None:
                break
        raise HTTPException(409, "Memory version conflict")

    @router.delete("/api/memory/{name:path}")
    async def delete_memory(name: str, _: Principal = user) -> dict:
        if not await store.delete_topic(topic_name(name)):
            raise HTTPException(404, "Topic not found")
        return {"deleted": True}

    # ---- automations -----------------------------------------------------

    def automation_key(schedule_id: str) -> str:
        return f"automation:{schedule_id}"

    async def render_schedule(s: Schedule) -> dict[str, Any]:
        runs = await store.list_runs(automation_key(s.id), limit=1)
        last = ({"status": str(runs[0].status), "finished_at": _iso(runs[0].finished_at),
                 "error": runs[0].error} if runs else None)
        return {"id": s.id, "name": s.name, "prompt": s.prompt, "spec": s.spec,
                "timezone": s.timezone, "enabled": s.enabled, "one_shot": s.one_shot,
                "next_run_at": _iso(s.next_run_at), "last_run_at": _iso(s.last_run_at),
                "created_by": s.created_by, "notify": s.notify,
                "thread_key": automation_key(s.id), "last_run": last}

    async def owned_schedule(schedule_id: str, principal: Principal) -> Schedule:
        schedule = await store.get_schedule(schedule_id)
        if schedule is None:
            raise HTTPException(404, "Automation not found")
        if not principal.is_admin and schedule.created_by != principal.user_id:
            raise HTTPException(403, "Only the owner or an admin can change this automation")
        return schedule

    def next_run(spec: str, tz: str) -> tuple[datetime | None, bool]:
        try:
            return parse_spec(spec, tz, scheduler.clock())
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @router.get("/api/automations")
    async def list_automations(_: Principal = user) -> list:
        return [await render_schedule(s) for s in await store.list_schedules(include_disabled=True)]

    @router.post("/api/automations")
    async def create_automation(body: AutomationCreate, principal: Principal = user) -> dict:
        nxt, one_shot = next_run(body.schedule, body.timezone)
        schedule = Schedule(
            name=body.name, prompt=body.prompt, spec=body.schedule, timezone=body.timezone,
            notify=body.notify, created_by=principal.user_id, next_run_at=nxt,
            one_shot=one_shot, created_at=scheduler.clock(),
        )
        await store.create_schedule(schedule)
        scheduler.wake()
        return await render_schedule(schedule)

    @router.patch("/api/automations/{schedule_id}")
    async def patch_automation(schedule_id: str, body: AutomationPatch,
                               principal: Principal = user) -> dict:
        schedule = await owned_schedule(schedule_id, principal)
        changes = body.model_dump(exclude_unset=True)
        if "schedule" in changes:
            changes["spec"] = changes.pop("schedule")
        for field in ("name", "prompt", "spec", "timezone", "notify", "enabled"):
            if field in changes and changes[field] is None:
                raise HTTPException(422, f"{field} cannot be null")
        for field, value in changes.items():
            setattr(schedule, field, value)
        if any(k in changes for k in ("spec", "timezone", "enabled")):
            nxt, one_shot = next_run(schedule.spec, schedule.timezone)  # always validated
            if schedule.enabled:
                schedule.next_run_at, schedule.one_shot = nxt, one_shot
        await store.update_schedule(schedule)
        scheduler.wake()
        return await render_schedule(schedule)

    @router.delete("/api/automations/{schedule_id}")
    async def delete_automation(schedule_id: str, principal: Principal = user) -> dict:
        await owned_schedule(schedule_id, principal)
        await store.delete_schedule(schedule_id)
        return {"deleted": True}

    @router.post("/api/automations/{schedule_id}/run")
    async def run_automation(schedule_id: str, principal: Principal = user) -> dict:
        schedule = await owned_schedule(schedule_id, principal)
        now = scheduler.clock()
        key = automation_key(schedule.id)
        event = Event(
            type="automation.fired", source="web",
            payload={"schedule_id": schedule.id, "name": schedule.name,
                     "prompt": schedule.prompt, "fired_at": now.isoformat(),
                     "notify": schedule.notify, "parent_thread_key": schedule.parent_thread_key,
                     "manual": True},
            idempotency_key=f"automation:{schedule.id}:manual:{uuid.uuid4().hex}",
            thread_key=key, user_id=schedule.created_by, received_at=now,
        )
        spec = ThreadSpec(key, ThreadType.AUTOMATION, Lane.BACKGROUND,
                          owner_user_id=schedule.created_by,
                          harness=schedule.harness or await resolve_harness(scheduler.default_harness),
                          model=schedule.model)
        await store.put(event, spec)
        return {"event_id": event.id, "thread_key": key}

    # ---- usage -----------------------------------------------------------

    @router.get("/api/usage")
    async def usage(days: int = 30, principal: Principal = user) -> dict:
        if not 1 <= days <= 3650:
            raise HTTPException(422, "days must be between 1 and 3650")
        since = scheduler.clock() - timedelta(days=days)
        threads = [t for t in await store.list_threads(limit=THREAD_SCAN_LIMIT)
                   if can_see_thread(principal, t.key)]
        runs: list[Run] = []
        for thread in threads:
            for run in await store.list_runs(thread.key, limit=RUN_SCAN_LIMIT):
                if run.started_at >= since:
                    runs.append(run)
        return aggregate_usage(runs, {t.key: str(t.type) for t in threads}, days)

    # ---- tools -----------------------------------------------------------

    @router.get("/api/tools")
    async def tools(principal: Principal = user) -> list:
        ctx = RunContext(run_id="", thread_key=f"chat:{principal.user_id}:tools",
                         user_id=principal.user_id, acting_as=principal.user_id)
        return [{"name": s.name, "action": s.action, "risk": str(s.risk),
                 "description": s.description, "decision": str(gateway.decide(ctx, s)[0])}
                for s in gateway.specs()]

    # ---- uploads ---------------------------------------------------------

    def upload_owner(principal: Principal) -> str:
        if principal.user_id is None:
            raise HTTPException(400, "X-Handler-User is required for admin-key uploads")
        return principal.user_id

    def visible_upload(file_id: str, principal: Principal):
        meta = uploads.get(file_id)
        if meta is None or not (principal.is_admin or meta.user_id == principal.user_id):
            raise HTTPException(404, "File not found")
        return meta

    @router.post("/api/uploads")
    async def upload(files: list[UploadFile] = File(...), principal: Principal = user) -> list:
        owner = upload_owner(principal)
        if len(files) > MAX_FILES_PER_REQUEST:
            raise HTTPException(422, f"At most {MAX_FILES_PER_REQUEST} files per request")
        saved = []
        try:
            for f in files:
                data = await f.read(MAX_FILE_BYTES + 1)
                try:
                    saved.append(uploads.save(owner, f.filename, f.content_type, [data]))
                except UploadTooLarge:
                    raise HTTPException(
                        413, f"{f.filename or 'file'} is larger than {MAX_FILE_BYTES} bytes")
        except HTTPException:
            for meta in saved:
                uploads.delete(meta)
            raise
        return [m.public() for m in saved]

    @router.get("/api/uploads")
    async def list_uploads(principal: Principal = user) -> list:
        files = uploads.list() if principal.is_admin else uploads.list(principal.user_id)
        return [{**m.public(), "user_id": m.user_id} for m in files]

    @router.get("/api/uploads/{file_id}")
    async def download_upload(file_id: str, principal: Principal = user) -> FileResponse:
        meta = visible_upload(file_id, principal)
        path = uploads.path(meta)
        if path is None:
            raise HTTPException(404, "File not found")
        return FileResponse(path, media_type=meta.content_type, filename=meta.filename)

    @router.delete("/api/uploads/{file_id}")
    async def delete_upload(file_id: str, principal: Principal = user) -> dict:
        uploads.delete(visible_upload(file_id, principal))
        return {"deleted": True}

    # ---- logs ------------------------------------------------------------

    @router.get("/api/logs")
    async def logs(lines: int = 200, level: str = "", q: str = "",
                   _: Principal = admin) -> list:
        if log_file is None:
            return []
        wanted = LOG_LEVELS.get(level.upper()) if level else None
        if level and wanted is None:
            raise HTTPException(422, f"Unknown level: {level}")
        lines = max(1, min(lines, LOG_TAIL_MAX_LINES))
        needle = q.lower()
        out: list[dict] = []
        # Filters apply before the cut, so scan more than `lines` when filtering.
        scan = lines if not (wanted or needle) else LOG_TAIL_MAX_LINES * 4
        for raw in _tail(log_file, scan):
            if needle and needle not in raw.lower():
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if wanted is not None and LOG_LEVELS.get(str(entry.get("level")), 0) < wanted:
                continue
            out.append(entry)
        return out[-lines:]

    # ---- connections -----------------------------------------------------

    @router.get("/api/connections")
    async def connections(principal: Principal = user) -> list:
        owner = None if principal.is_admin else f"user:{principal.user_id}"
        return [{"id": c.id, "connector": c.connector, "owner": c.owner,
                 "provider": c.provider, "status": c.status, "scopes": c.scopes,
                 "last_used_at": _iso(c.last_used_at)}
                for c in await store.list_connections(owner)]

    return router
