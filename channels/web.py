"""Web channel: FastAPI chat UI that pushes events into the environment queue."""

import asyncio
import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..environment import Channel
from ..types import Event
from ..event_store import EventStore
from .admin import create_admin_router

logger = logging.getLogger("handler.channels.web")

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_SESSION_COOKIE = "hcid"  # handler conversation id


class _ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


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
    ):
        self.store = store
        self.host = host
        self.port = port
        self.queue: asyncio.Queue | None = None
        self.app = self._build_app(
            memory=memory, config_dir=config_dir, tools=tools,
        )

    def _new_conversation_id(self) -> str:
        cid = "web-" + uuid.uuid4().hex[:12]
        self.store.ensure_conversation(cid, channel="web")
        return cid

    def _build_app(self, memory, config_dir, tools) -> FastAPI:
        app = FastAPI(title="Handler")
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
        channel = self  # capture for closures

        # Admin dashboard endpoints (memory, config, cron, logs, files, tools, etc.)
        admin_router = create_admin_router(
            store=channel.store,
            memory=memory,
            config_dir=config_dir,
            tools=tools,
        )
        app.include_router(admin_router)

        # --- Chat ---

        @app.get("/")
        async def index():
            return FileResponse(str(_STATIC_DIR / "index.html"))

        @app.get("/api/conversations")
        async def conversations_list():
            return {"conversations": channel.store.list_web_conversations()}

        @app.post("/api/conversations")
        async def conversations_new(response: Response):
            cid = channel._new_conversation_id()
            response.set_cookie(_SESSION_COOKIE, cid, samesite="strict", httponly=True)
            return {"conversation_id": cid}

        @app.get("/api/history")
        async def history(request: Request, cid: str | None = None):
            conversation_id = cid or request.cookies.get(_SESSION_COOKIE) or ""
            if not conversation_id:
                return {"messages": [], "conversation_id": ""}
            messages = channel.store.get_messages(conversation_id)
            return {"messages": messages, "conversation_id": conversation_id}

        @app.post("/api/chat")
        async def chat(req: _ChatRequest, request: Request, response: Response):
            # Resolve or create conversation
            conversation_id = (
                req.conversation_id
                or request.cookies.get(_SESSION_COOKIE)
            )
            if not conversation_id:
                conversation_id = channel._new_conversation_id()
            else:
                # Ensure it exists in DB
                channel.store.ensure_conversation(conversation_id, channel="web")
            response.set_cookie(_SESSION_COOKIE, conversation_id, samesite="strict", httponly=True)

            future = asyncio.get_running_loop().create_future()
            event = Event(
                type="user_message",
                source="web",
                data={"content": req.message},
                conversation_id=conversation_id,
                _response_future=future,
            )
            await channel.queue.put(event)
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

