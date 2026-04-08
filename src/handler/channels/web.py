"""Web channel: FastAPI chat UI that pushes events into the environment queue."""

import asyncio
import base64
import json
import logging
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..environment import Channel
from ..types import Event
from ..event_store import EventStore
from ..paths import UPLOAD_DIR
from .admin import create_admin_router

logger = logging.getLogger("handler.channels.web")

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class _ImageData(BaseModel):
    data: str  # base64-encoded image data
    media_type: str = "image/jpeg"  # MIME type


class _ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    images: list[_ImageData] | None = None


class WebChannel(Channel):
    """FastAPI-based web chat UI."""

    name = "web"

    def __init__(
        self,
        store: EventStore,
        host: str = "0.0.0.0",
        port: int = 8000,
        memory=None,
        config_dir: Path | None = None,
        tools: list | None = None,
        agent_config_loader=None,
        agent_swapper=None,
    ):
        self.store = store
        self.host = host
        self.port = port
        self.queue: asyncio.Queue | None = None
        self.listeners: dict[str, set[asyncio.Queue]] = {}
        self.app = self._build_app(
            memory=memory,
            config_dir=config_dir,
            tools=tools,
            agent_config_loader=agent_config_loader,
            agent_swapper=agent_swapper,
        )

    def _new_conversation_id(self) -> str:
        cid = "web-" + uuid.uuid4().hex[:12]
        self.store.ensure_conversation(cid, channel="web")
        return cid

    async def push_message(self, conversation_id: str, role: str, content: str) -> None:
        queues = list(self.listeners.get(conversation_id, set()))
        if not queues:
            logger.debug(f"push_message: no listeners for {conversation_id}")
            return
        payload = {"conversation_id": conversation_id, "role": role, "content": content}
        stale: list[asyncio.Queue] = []
        for q in queues:
            try:
                q.put_nowait(payload)
            except Exception:
                stale.append(q)
        for q in stale:
            current = self.listeners.get(conversation_id, set())
            current.discard(q)
            if not current:
                self.listeners.pop(conversation_id, None)

    def _build_app(
        self, memory, config_dir, tools, agent_config_loader, agent_swapper
    ) -> FastAPI:
        app = FastAPI(title="Handler")
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
        channel = self  # capture for closures

        admin_router = create_admin_router(
            store=channel.store,
            memory=memory,
            config_dir=config_dir,
            tools=tools,
            agent_config_loader=agent_config_loader,
            agent_swapper=agent_swapper,
        )
        app.include_router(admin_router)

        @app.get("/")
        async def index():
            return FileResponse(str(_STATIC_DIR / "index.html"))

        @app.get("/api/uploads/{filename}")
        async def serve_upload(filename: str):
            """Serve uploaded files (images) for display in the web UI."""
            path = UPLOAD_DIR / filename
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "not found"}, status_code=404)
            return FileResponse(str(path))

        @app.get("/api/history")
        async def history(cid: str | None = None):
            if not cid:
                return {"messages": [], "conversation_id": ""}
            messages = channel.store.get_messages(cid, include_compacted=True)
            return {"messages": messages, "conversation_id": cid}

        @app.get("/api/stream")
        async def stream(cid: str):
            channel.store.ensure_conversation(cid, channel="web")

            async def event_generator():
                q: asyncio.Queue = asyncio.Queue()
                channel.listeners.setdefault(cid, set()).add(q)
                logger.info(
                    f"SSE listener connected: {cid} (total: {sum(len(v) for v in channel.listeners.values())})"
                )
                try:
                    yield {
                        "event": "ready",
                        "data": json.dumps({"conversation_id": cid}),
                    }
                    while True:
                        payload = await q.get()
                        yield {"event": "message", "data": json.dumps(payload)}
                except asyncio.CancelledError:
                    raise
                finally:
                    listeners = channel.listeners.get(cid, set())
                    listeners.discard(q)
                    if not listeners:
                        channel.listeners.pop(cid, None)
                    logger.info(
                        f"SSE listener disconnected: {cid} (total: {sum(len(v) for v in channel.listeners.values())})"
                    )

            return EventSourceResponse(event_generator())

        @app.post("/api/chat")
        async def chat(req: _ChatRequest):
            conversation_id = req.conversation_id
            if not conversation_id:
                conversation_id = channel._new_conversation_id()
            else:
                channel.store.ensure_conversation(conversation_id, channel="web")

            # Save any uploaded images to disk and build image references
            images = None
            if req.images:
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                images = []
                for img in req.images:
                    ext = img.media_type.split("/")[-1].replace("jpeg", "jpg")
                    fname = f"web_{uuid.uuid4().hex[:8]}.{ext}"
                    dest = UPLOAD_DIR / fname
                    dest.write_bytes(base64.b64decode(img.data))
                    images.append(
                        {"path": str(dest.resolve()), "media_type": img.media_type}
                    )
                    logger.info(f"chat image saved: {dest.resolve()}")

            future = asyncio.get_running_loop().create_future()
            event_data: dict[str, object] = {"content": req.message}
            if images:
                event_data["images"] = images
            event = Event(
                type="user_message",
                source="web",
                data=event_data,
                conversation_id=conversation_id,
                _response_future=future,
            )
            queue = channel.queue
            if queue is None:
                return JSONResponse(
                    {"error": "web channel not started"}, status_code=503
                )
            await queue.put(event)
            try:
                resp = await future
                return {"response": resp, "conversation_id": conversation_id}
            except Exception as e:
                return {"error": str(e), "conversation_id": conversation_id}

        return app

    async def start(self, queue: asyncio.Queue) -> None:
        self.queue = queue

        import uvicorn

        config = uvicorn.Config(
            self.app, host=self.host, port=self.port, log_level="info"
        )
        server = uvicorn.Server(config)
        await server.serve()
