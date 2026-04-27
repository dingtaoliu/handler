"""Admin API router: memory CRUD, config editing, cron management, logs, files, tools."""

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..event_store import EventStore
from ..memory import _validate_topic
from ..paths import UPLOAD_DIR, LOG_DIR, get_log_path, PROJECT_ROOT, PID_PATH

logger = logging.getLogger("handler.channels.admin")


class _WriteBody(BaseModel):
    content: str


def _safe_upload_filename(name: str | None) -> str:
    """Normalize uploaded filenames to a single safe path component."""
    if not name:
        return f"upload-{uuid.uuid4().hex}"

    safe = Path(name).name
    if not safe or safe in {".", ".."}:
        return f"upload-{uuid.uuid4().hex}"
    return safe


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


def _validate_md_filename(name: str) -> str | None:
    """Validate a .md filename for admin API endpoints. Returns safe name or None."""
    try:
        return _validate_topic(name)
    except ValueError:
        return None


class _AgentBody(BaseModel):
    backend: str
    model: str


def create_admin_router(
    store: EventStore,
    memory=None,
    config_dir: Path | None = None,
    tools: list | None = None,
    agent_config_loader=None,
    agent_swapper=None,
) -> APIRouter:
    """Build a FastAPI router with all admin/dashboard endpoints."""
    router = APIRouter(prefix="/api")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # --- Tokens ---

    @router.get("/tokens")
    async def tokens(days: int | None = None):
        return store.get_token_summary(days=days)

    # --- Agent config ---

    @router.get("/agent")
    async def agent_config():
        if not agent_config_loader:
            return {"backend": "openai", "model": "gpt-5.4-2026-03-05"}
        return agent_config_loader()

    @router.put("/agent")
    async def agent_update(body: _AgentBody):
        valid_backends = ["openai", "openai-manual", "claude", "anthropic"]
        if body.backend not in valid_backends:
            return JSONResponse(
                {"error": f"invalid backend, must be one of: {valid_backends}"},
                status_code=400,
            )
        if not body.model.strip():
            return JSONResponse({"error": "model is required"}, status_code=400)
        if not agent_swapper:
            return JSONResponse(
                {"error": "agent swapping not available"}, status_code=503
            )
        agent_swapper(body.backend, body.model.strip())
        logger.info(f"agent config updated: backend={body.backend}, model={body.model}")
        return {"ok": True, "backend": body.backend, "model": body.model}

    # --- Upload ---

    @router.post("/upload")
    async def upload(files: list[UploadFile] = File(...)):
        results = []
        for f in files:
            filename = _safe_upload_filename(f.filename)
            dest = UPLOAD_DIR / filename
            content = await f.read()
            dest.write_bytes(content)
            logger.info(
                f"upload: {filename} ({len(content)} bytes) -> {dest.resolve()}"
            )
            results.append({"name": filename, "path": str(dest.resolve())})
        return {"files": results}

    # --- Recovery ---

    @router.get("/recover")
    async def recover(token: str = ""):
        """Emergency recovery: git reset --hard HEAD + remove PID for watchdog restart."""
        import os
        import subprocess

        expected = os.environ.get("HANDLER_RECOVER_TOKEN", "")
        if not expected:
            return {
                "error": "recovery endpoint disabled (HANDLER_RECOVER_TOKEN not set)"
            }
        if token != expected:
            return {"error": "invalid token"}

        result = subprocess.run(
            ["git", "reset", "--hard", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return {"error": f"git reset failed: {result.stderr.strip()}"}

        logger.info("recover: git reset --hard HEAD succeeded, queuing restart")
        PID_PATH.unlink(missing_ok=True)
        return {
            "ok": True,
            "reset_to": result.stdout.strip(),
            "note": "PID file removed — watchdog will restart handler within 60s",
        }

    # --- Memory ---

    @router.get("/memory")
    async def memory_list():
        if not memory:
            return {"files": []}
        return {"files": memory.list_topics()}

    @router.get("/memory/{name}")
    async def memory_read(name: str):
        safe = _validate_md_filename(name)
        if not safe:
            return JSONResponse({"error": "invalid filename"}, status_code=400)
        if not memory:
            return JSONResponse({"error": "memory not available"}, status_code=503)
        return {"filename": safe, "content": memory.read(safe)}

    @router.put("/memory/{name}")
    async def memory_write(name: str, body: _WriteBody):
        safe = _validate_md_filename(name)
        if not safe:
            return JSONResponse({"error": "invalid filename"}, status_code=400)
        if not memory:
            return JSONResponse({"error": "memory not available"}, status_code=503)
        memory.write(safe, body.content)
        return {"ok": True, "filename": safe}

    @router.delete("/memory/{name}")
    async def memory_delete(name: str):
        safe = _validate_md_filename(name)
        if not safe:
            return JSONResponse({"error": "invalid filename"}, status_code=400)
        if not memory:
            return JSONResponse({"error": "memory not available"}, status_code=503)
        return {"ok": memory.delete(safe)}

    # --- Config ---

    @router.get("/config")
    async def config_list():
        if not config_dir:
            return {"files": []}
        files = []
        for fname in ("identity.md", "persona.md"):
            p = config_dir / fname
            files.append({"name": fname, "exists": p.exists()})
        return {"files": files}

    @router.get("/config/{name}")
    async def config_read(name: str):
        safe = _validate_md_filename(name)
        if not safe:
            return JSONResponse({"error": "invalid filename"}, status_code=400)
        if not config_dir:
            return JSONResponse({"error": "config not available"}, status_code=503)
        p = config_dir / safe
        return {"name": safe, "content": p.read_text() if p.exists() else ""}

    @router.put("/config/{name}")
    async def config_write(name: str, body: _WriteBody):
        safe = _validate_md_filename(name)
        if not safe:
            return JSONResponse({"error": "invalid filename"}, status_code=400)
        if not config_dir:
            return JSONResponse({"error": "config not available"}, status_code=503)
        p = config_dir / safe
        p.write_text(body.content)
        logger.info(f"config write: {safe} ({len(body.content)} chars)")
        return {"ok": True}

    # --- Cron ---

    @router.get("/cron")
    async def cron_list():
        return {"jobs": store.list_cron_jobs()}

    @router.delete("/cron/{job_id}")
    async def cron_delete(job_id: int):
        return {"ok": store.delete_cron_job(job_id)}

    # --- Logs ---

    @router.get("/logs")
    async def logs(lines: int = 100, date: str | None = None):
        from datetime import date as date_cls
        import re

        if date:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                return JSONResponse({"error": "invalid date"}, status_code=400)
            try:
                d = date_cls.fromisoformat(date)
            except ValueError:
                return JSONResponse({"error": "invalid date"}, status_code=400)
            log_path = get_log_path(d)
        else:
            log_path = get_log_path()
        return {"lines": _tail_file(log_path, max(1, min(lines, 2000)))}

    @router.get("/logs/dates")
    async def logs_dates():
        """List available log dates (newest first)."""
        dates = []
        if LOG_DIR.exists():
            for p in sorted(LOG_DIR.glob("handler-*.log"), reverse=True):
                stem = p.stem  # handler-2026-03-15
                d = stem.removeprefix("handler-")
                dates.append(d)
        return {"dates": dates}

    # --- Files ---

    @router.get("/files")
    async def files_list():
        results = []
        if UPLOAD_DIR.exists():
            for p in sorted(
                UPLOAD_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True
            ):
                if p.is_file():
                    st = p.stat()
                    results.append(
                        {"name": p.name, "size": st.st_size, "modified": st.st_mtime}
                    )
        return {"files": results}

    @router.delete("/files/{name}")
    async def files_delete(name: str):
        safe = Path(name).name
        if not safe or ".." in safe:
            return JSONResponse({"error": "invalid filename"}, status_code=400)
        p = UPLOAD_DIR / safe
        if not p.exists() or not p.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        p.unlink()
        return {"ok": True}

    # --- Conversations ---

    @router.get("/conversations")
    async def conversations_list():
        return {"conversations": store.list_web_conversations()}

    @router.post("/conversations")
    async def conversations_new():
        cid = "web-" + uuid.uuid4().hex[:12]
        store.ensure_conversation(cid, channel="web")
        return {"conversation_id": cid}

    # --- Sessions (all channels) ---

    @router.get("/sessions")
    async def sessions_list():
        return {"sessions": store.list_all_conversations()}

    # --- Tools ---

    @router.get("/tools")
    async def tools_list():
        result = []
        for t in tools or []:
            try:
                name = getattr(t, "name", None) or getattr(
                    t, "__name__", type(t).__name__
                )
                desc = getattr(t, "description", "") or ""
                result.append(
                    {"name": name, "description": desc.strip().split("\n")[0]}
                )
            except Exception:
                pass
        return {"tools": result}

    return router
