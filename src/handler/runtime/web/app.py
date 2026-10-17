"""Web API and run timeline for the handler runtime (phase 1, local only)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..bus import BusMessage, RunEventBus
from ..store.base import Store
from ..types import Event, Lane, ThreadSpec, ThreadType

HEARTBEAT_SECONDS = 15.0
TIMELINE_HTML = Path(__file__).resolve().parent / "static" / "timeline.html"


class ChatRequest(BaseModel):
    user_id: str
    text: str
    conversation_id: str | None = None
    client_message_id: str | None = None


def _json(obj: Any) -> Any:
    return jsonable_encoder(obj)


async def sse_stream(
    bus: RunEventBus,
    thread_key: str,
    request: Request | None = None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Yield SSE frames for bus messages, with comment heartbeats when idle."""
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
                yield ": heartbeat\n\n"
                continue
            yield f"data: {json.dumps(_json(msg))}\n\n"


def create_app(
    store: Store, bus: RunEventBus, *, default_harness: str = "codex"
) -> FastAPI:
    app = FastAPI(title="handler runtime")
    # TODO(auth): phase 1 is local only; add authentication before exposing.
    router = APIRouter()

    @router.post("/api/chat")
    async def chat(body: ChatRequest) -> dict[str, Any]:
        conversation_id = body.conversation_id or uuid.uuid4().hex
        key = f"chat:{body.user_id}:{conversation_id}"
        event = Event(
            type="chat.message",
            source="web",
            payload={"text": body.text},
            idempotency_key=body.client_message_id or uuid.uuid4().hex,
            user_id=body.user_id,
            thread_key=key,
        )
        spec = ThreadSpec(
            key=key,
            type=ThreadType.CHAT,
            lane=Lane.INTERACTIVE,
            owner_user_id=body.user_id,
            harness=default_harness,
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
    async def list_threads(user_id: str | None = None) -> Any:
        return _json(await store.list_threads(owner_user_id=user_id))

    @router.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> Any:
        run = await store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        steps = await store.list_steps(run_id)
        return {"run": _json(run), "steps": _json(steps)}

    @router.get("/api/stream")
    async def stream_all(request: Request) -> StreamingResponse:
        return _sse_response(request, "*")

    # Suffixed routes must precede the greedy bare {key:path} route.
    @router.get("/api/threads/{key:path}/stream")
    async def stream_thread(key: str, request: Request) -> StreamingResponse:
        return _sse_response(request, key)

    @router.get("/api/threads/{key:path}/messages")
    async def thread_messages(key: str) -> Any:
        return _json(await store.list_messages(key))

    @router.get("/api/threads/{key:path}/runs")
    async def thread_runs(key: str) -> Any:
        return _json(await store.list_runs(key))

    @router.get("/api/threads/{key:path}")
    async def get_thread(key: str) -> Any:
        thread = await store.get_thread(key)
        if thread is None:
            raise HTTPException(404, "thread not found")
        pending = await store.pending_events(key)
        return {"thread": _json(thread), "pending_events": _json(pending)}

    def _sse_response(request: Request, key: str) -> StreamingResponse:
        return StreamingResponse(
            sse_stream(bus, key, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/timeline")
    async def timeline() -> FileResponse:
        return FileResponse(TIMELINE_HTML, media_type="text/html")

    app.include_router(router)
    return app
