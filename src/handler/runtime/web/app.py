"""Web API and run timeline for the handler runtime (phase 1, local only)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from ..auth import ACT_AS_HEADER, Authenticator, Principal, can_see_thread
from ..bus import BusMessage, RunEventBus
from ..store.base import Store
from ..uploads import UploadStore
from ..run import fill_tool_inputs
from ..settings import HarnessSource, ModelName, ReasoningEffort, resolve_harness
from ..types import Event, Lane, ThreadSpec, ThreadType
from .auth_api import auth_router

HEARTBEAT_SECONDS = 15.0
MEMBER_SCAN_LIMIT = 1000  # threads scanned when filtering a member's list
TIMELINE_HTML = Path(__file__).resolve().parent / "static" / "timeline.html"
APP_DIR = Path(__file__).resolve().parent / "static" / "app"


class ChatRequest(BaseModel):
    # The user comes from the principal, never from the body.
    text: str
    attachments: list[str] = []  # ids of the caller's own uploads
    conversation_id: str | None = None
    client_message_id: str | None = None
    model: ModelName | None = None  # per-message override; validated against the enum
    reasoning_effort: ReasoningEffort | None = None


def _json(obj: Any) -> Any:
    return jsonable_encoder(obj)


async def sse_stream(
    bus: RunEventBus,
    thread_key: str,
    request: Request | None = None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    visible: Callable[[str], bool] | None = None,
    authorized: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames for bus messages, with comment heartbeats when idle.

    `visible` filters messages by thread key (used to hide other users' chats).
    """
    async with bus.subscribe(thread_key) as queue:
        yield ": connected\n\n"
        while True:
            try:
                msg: BusMessage = await asyncio.wait_for(
                    queue.get(), timeout=heartbeat_seconds
                )
            except asyncio.TimeoutError:
                if request is not None and await request.is_disconnected():
                    return
                if authorized is not None and not await authorized():
                    return
                yield ": heartbeat\n\n"
                continue
            if authorized is not None and not await authorized():
                return
            if visible is None or visible(msg.thread_key):
                yield f"data: {json.dumps(_json(msg))}\n\n"


def create_app(
    store: Store, bus: RunEventBus, auth: Authenticator, *, default_harness: HarnessSource = "codex",
    uploads: UploadStore | None = None,
    instance_name: str = "",
) -> FastAPI:
    # No interactive docs / OpenAPI: the API is authenticated, its schema needn't be public.
    app = FastAPI(title="handler runtime", docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter()
    principal_dep = Depends(auth.require_principal)
    sse_dep = Depends(auth.require_sse_principal)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    def visible_thread(principal: Principal, key: str) -> None:
        # 404 rather than 403 so other users' thread keys aren't confirmed.
        if not can_see_thread(principal, key):
            raise HTTPException(404, "thread not found")

    @router.post("/api/chat")
    async def chat(body: ChatRequest, request: Request,
                   principal: Principal = principal_dep) -> dict[str, Any]:
        user_id = principal.user_id
        if user_id is None:
            raise HTTPException(400, f"{ACT_AS_HEADER} is required for admin-key chat")
        payload: dict[str, Any] = {"text": body.text}
        if body.model is not None:
            payload["model"] = body.model.value
        if body.reasoning_effort is not None:
            payload["reasoning_effort"] = body.reasoning_effort.value
        if body.attachments:
            if uploads is None:
                raise HTTPException(400, "attachments are not enabled")
            for file_id in body.attachments:
                meta = uploads.get(file_id)
                if meta is None or meta.user_id != user_id:
                    raise HTTPException(404, f"attachment not found: {file_id}")
            payload["attachments"] = list(body.attachments)
        conversation_id = body.conversation_id or uuid.uuid4().hex
        key = f"chat:{user_id}:{conversation_id}"
        event = Event(
            type="chat.message",
            source="web",
            payload=payload,
            idempotency_key=f"{user_id}:{body.client_message_id or uuid.uuid4().hex}",
            user_id=user_id,
            thread_key=key,
        )
        spec = ThreadSpec(
            key=key,
            type=ThreadType.CHAT,
            lane=Lane.INTERACTIVE,
            owner_user_id=user_id,
            harness=await resolve_harness(default_harness),
        )
        stored = await store.put(event, spec)
        if stored:
            bus.publish(
                BusMessage(
                    thread_key=key,
                    run_id=None,
                    type="event_queued",
                    data={"event_id": event.id},
                )
            )
        return {
            "event_id": event.id,
            "thread_key": key,
            "conversation_id": conversation_id,
            "duplicate": not stored,
        }

    @router.get("/api/threads")
    async def list_threads(user_id: str | None = None,
                           principal: Principal = principal_dep) -> Any:
        if principal.is_admin:
            return _json(await store.list_threads(owner_user_id=user_id))
        threads = await store.list_threads(owner_user_id=user_id, limit=MEMBER_SCAN_LIMIT)
        return _json([t for t in threads if can_see_thread(principal, t.key)][:100])

    @router.get("/api/events")
    async def list_events(source: str | None = None, status: str | None = None,
                          limit: int = 50, principal: Principal = principal_dep) -> Any:
        """Recent events (e.g. ignored emails for Activity), newest first."""
        rows = await store.list_events(source=source, status=status,
                                       limit=max(1, min(limit, 200)))
        return _json([{**_json(event), "status": state} for event, state in rows
                      if not event.thread_key or can_see_thread(principal, event.thread_key)])

    @router.get("/api/runs/{run_id}")
    async def get_run(run_id: str, principal: Principal = principal_dep) -> Any:
        run = await store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if not can_see_thread(principal, run.thread_key):
            raise HTTPException(404, "run not found")
        steps = await store.list_steps(run_id)
        return {"run": _json(run), "steps": _json(steps)}

    @router.get("/api/stream")
    async def stream_all(request: Request,
                         principal: Principal = sse_dep) -> StreamingResponse:
        return _sse_response(request, "*", principal)

    # Suffixed routes must precede the greedy bare {key:path} route.
    @router.get("/api/threads/{key:path}/stream")
    async def stream_thread(key: str, request: Request,
                            principal: Principal = sse_dep) -> StreamingResponse:
        visible_thread(principal, key)
        return _sse_response(request, key, principal)

    @router.get("/api/threads/{key:path}/messages")
    async def thread_messages(key: str, principal: Principal = principal_dep) -> Any:
        visible_thread(principal, key)
        return _json(await fill_tool_inputs(store, await store.list_messages(key)))

    @router.get("/api/threads/{key:path}/runs")
    async def thread_runs(key: str, principal: Principal = principal_dep) -> Any:
        visible_thread(principal, key)
        return _json(await store.list_runs(key))

    @router.get("/api/threads/{key:path}")
    async def get_thread(key: str, principal: Principal = principal_dep) -> Any:
        visible_thread(principal, key)
        thread = await store.get_thread(key)
        if thread is None:
            raise HTTPException(404, "thread not found")
        pending = await store.pending_events(key)
        return {"thread": _json(thread), "pending_events": _json(pending)}

    def _sse_response(request: Request, key: str, principal: Principal) -> StreamingResponse:
        async def authorized() -> bool:
            # Revocation, disabling and role changes apply to already open streams.
            try:
                return await auth.require_sse_principal(request) == principal
            except HTTPException:
                return False

        return StreamingResponse(
            sse_stream(bus, key, request, visible=lambda k: can_see_thread(principal, k),
                       authorized=authorized),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def html(path: Path, request: Request) -> HTMLResponse:
        base = request.scope.get("root_path", "").rstrip("/")
        # Serialize trusted server config, keeping script contexts safe even under
        # an externally supplied ASGI root path.
        config = json.dumps({"base": base, "instance": instance_name}).replace("<", "\\u003c")
        bootstrap = f"<script>window.HANDLER_CONFIG={config};</script>"
        content = path.read_text().replace("<script", bootstrap + "<script", 1)
        content = content.replace('href="/', f'href="{base}/').replace('src="/', f'src="{base}/')
        return HTMLResponse(content, headers={"Cache-Control": "no-store"})

    @router.get("/")
    @router.get("/app")
    @router.get("/app/")
    async def app_shell(request: Request) -> HTMLResponse:
        # Unauthenticated shell: it redirects to /login without a token, and
        # every data call carries one.
        return html(APP_DIR / "index.html", request)

    @router.get("/app/static/{name}")
    async def app_asset(name: str) -> FileResponse:
        path = APP_DIR / name
        if name != path.name or name == "index.html" or not path.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    @router.get("/login")
    async def login(request: Request) -> HTMLResponse:
        return html(TIMELINE_HTML.with_name("login.html"), request)

    @router.get("/timeline")
    async def timeline(request: Request, _: Principal = principal_dep) -> HTMLResponse:
        return html(TIMELINE_HTML, request)

    app.include_router(router)
    app.include_router(auth_router(auth))
    return app
