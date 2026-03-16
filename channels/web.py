"""Web channel: FastAPI chat UI that pushes events into the environment queue."""

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..environment import Channel
from ..types import Event
from ..event_store import EventStore

logger = logging.getLogger("handler.channels.web")

UPLOAD_DIR = Path("./data/uploads")
_STATIC_DIR = Path(__file__).resolve().parent / "static"


class _ChatRequest(BaseModel):
    message: str


class _WriteBody(BaseModel):
    content: str


def _validate_md_filename(name: str) -> str | None:
    """Sanitize to bare filename. Returns safe .md name or None if invalid."""
    import re
    name = Path(name).name  # strip any path components
    if not name or ".." in name:
        return None
    if not name.endswith(".md"):
        name += ".md"
    if not re.match(r"^[\w][\w\-\.]*\.md$", name) or len(name) > 120:
        return None
    return name


def _tail_file(path: Path, n: int) -> list[str]:
    """Return last n lines of a file efficiently."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            buf = min(size, n * 200)
            f.seek(max(0, size - buf))
            content = f.read().decode("utf-8", errors="replace")
            return content.splitlines()[-n:]
    except Exception:
        return []


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
        self.conversation_id = "web"
        self.host = host
        self.port = port
        self.memory = memory
        self.config_dir = config_dir
        self.tools = tools
        self.queue: asyncio.Queue | None = None
        self.app = self._build_app()

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Handler")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
        channel = self  # capture for closures

        @app.get("/")
        async def index():
            return FileResponse(str(_STATIC_DIR / "index.html"))

        @app.get("/api/history")
        async def history():
            messages = channel.store.get_messages(channel.conversation_id)
            return {"messages": messages}

        @app.get("/api/tokens")
        async def tokens(days: int | None = None):
            """Token usage summary. Optional ?days=N to limit to last N days."""
            return channel.store.get_token_summary(days=days)

        @app.post("/api/upload")
        async def upload(files: list[UploadFile] = File(...)):
            results = []
            for f in files:
                dest = UPLOAD_DIR / f.filename
                content = await f.read()
                dest.write_bytes(content)
                logger.info(
                    f"upload: {f.filename} ({len(content)} bytes) -> {dest.resolve()}"
                )
                results.append({"name": f.filename, "path": str(dest.resolve())})
            return {"files": results}

        @app.get("/api/recover")
        async def recover(token: str = ""):
            """Emergency recovery endpoint. Resets all tracked handler/ files to
            the last git HEAD and queues a restart via the watchdog.

            Requires HANDLER_RECOVER_TOKEN env var to be set and matched.
            If the env var is not set, the endpoint is disabled.

            Usage: GET /api/recover?token=<your-token>
            """
            import os
            import subprocess
            from pathlib import Path

            expected = os.environ.get("HANDLER_RECOVER_TOKEN", "")
            if not expected:
                return {
                    "error": "recovery endpoint disabled (HANDLER_RECOVER_TOKEN not set)"
                }
            if token != expected:
                return {"error": "invalid token"}

            project_root = str(Path(__file__).resolve().parent.parent.parent)
            result = subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                cwd=project_root,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return {"error": f"git reset failed: {result.stderr.strip()}"}

            logger.info("recover: git reset --hard HEAD succeeded, queuing restart")
            # Remove PID file so watchdog restarts the handler on next tick
            pid_path = Path(__file__).resolve().parent.parent / "data" / "handler.pid"
            pid_path.unlink(missing_ok=True)
            return {
                "ok": True,
                "reset_to": result.stdout.strip(),
                "note": "PID file removed — watchdog will restart handler within 60s",
            }

        @app.post("/api/chat")
        async def chat(req: _ChatRequest):
            future = asyncio.get_running_loop().create_future()
            event = Event(
                type="user_message",
                source="web",
                data={"content": req.message},
                conversation_id=channel.conversation_id,
                _response_future=future,
            )
            await channel.queue.put(event)
            try:
                response = await future
                return {"response": response}
            except Exception as e:
                return {"error": str(e)}

        # --- Memory ---

        @app.get("/api/memory")
        async def memory_list():
            if not channel.memory:
                return {"files": []}
            return {"files": channel.memory.list_files()}

        @app.get("/api/memory/{name}")
        async def memory_read(name: str):
            safe = _validate_md_filename(name)
            if not safe:
                return JSONResponse({"error": "invalid filename"}, status_code=400)
            if not channel.memory:
                return JSONResponse({"error": "memory not available"}, status_code=503)
            return {"filename": safe, "content": channel.memory.read(safe)}

        @app.put("/api/memory/{name}")
        async def memory_write(name: str, body: _WriteBody):
            safe = _validate_md_filename(name)
            if not safe:
                return JSONResponse({"error": "invalid filename"}, status_code=400)
            if not channel.memory:
                return JSONResponse({"error": "memory not available"}, status_code=503)
            channel.memory.write(safe, body.content)
            return {"ok": True, "filename": safe}

        @app.delete("/api/memory/{name}")
        async def memory_delete(name: str):
            safe = _validate_md_filename(name)
            if not safe:
                return JSONResponse({"error": "invalid filename"}, status_code=400)
            if not channel.memory:
                return JSONResponse({"error": "memory not available"}, status_code=503)
            return {"ok": channel.memory.delete(safe)}

        # --- Config ---

        @app.get("/api/config")
        async def config_list():
            if not channel.config_dir:
                return {"files": []}
            files = []
            for fname in ("identity.md", "persona.md"):
                p = channel.config_dir / fname
                files.append({"name": fname, "exists": p.exists()})
            return {"files": files}

        @app.get("/api/config/{name}")
        async def config_read(name: str):
            safe = _validate_md_filename(name)
            if not safe:
                return JSONResponse({"error": "invalid filename"}, status_code=400)
            if not channel.config_dir:
                return JSONResponse({"error": "config not available"}, status_code=503)
            p = channel.config_dir / safe
            return {"name": safe, "content": p.read_text() if p.exists() else ""}

        @app.put("/api/config/{name}")
        async def config_write(name: str, body: _WriteBody):
            safe = _validate_md_filename(name)
            if not safe:
                return JSONResponse({"error": "invalid filename"}, status_code=400)
            if not channel.config_dir:
                return JSONResponse({"error": "config not available"}, status_code=503)
            p = channel.config_dir / safe
            p.write_text(body.content)
            logger.info(f"config write: {safe} ({len(body.content)} chars)")
            return {"ok": True}

        # --- Cron ---

        @app.get("/api/cron")
        async def cron_list():
            return {"jobs": channel.store.list_cron_jobs()}

        @app.delete("/api/cron/{job_id}")
        async def cron_delete(job_id: int):
            return {"ok": channel.store.delete_cron_job(job_id)}

        # --- Logs ---

        @app.get("/api/logs")
        async def logs(lines: int = 100):
            log_path = Path(channel.store.db_path).parent / "handler.log"
            return {"lines": _tail_file(log_path, max(1, min(lines, 2000)))}

        # --- Files ---

        @app.get("/api/files")
        async def files_list():
            results = []
            if UPLOAD_DIR.exists():
                for p in sorted(UPLOAD_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True):
                    if p.is_file():
                        st = p.stat()
                        results.append({"name": p.name, "size": st.st_size, "modified": st.st_mtime})
            return {"files": results}

        @app.delete("/api/files/{name}")
        async def files_delete(name: str):
            safe = Path(name).name
            if not safe or ".." in safe:
                return JSONResponse({"error": "invalid filename"}, status_code=400)
            p = UPLOAD_DIR / safe
            if not p.exists() or not p.is_file():
                return JSONResponse({"error": "not found"}, status_code=404)
            p.unlink()
            return {"ok": True}

        # --- Tools ---

        @app.get("/api/tools")
        async def tools_list():
            result = []
            for t in (channel.tools or []):
                try:
                    name = getattr(t, "name", None) or getattr(t, "__name__", type(t).__name__)
                    desc = getattr(t, "description", "") or ""
                    result.append({"name": name, "description": desc.strip().split("\n")[0]})
                except Exception:
                    pass
            return {"tools": result}

        return app

    async def start(self, queue: asyncio.Queue) -> None:
        self.queue = queue

        import uvicorn

        config = uvicorn.Config(
            self.app, host=self.host, port=self.port, log_level="info"
        )
        server = uvicorn.Server(config)
        await server.serve()
