from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any, AsyncIterator

from ..types import TokenUsage
from .base import HarnessError, McpServerSpec, RunEvent, RunEventKind, RunRequest

logger = logging.getLogger(__name__)
_STDERR_BYTES = 4096
_TOOL_TYPES = {"command_execution", "mcp_tool_call", "file_change", "web_search"}

SANDBOX_MODES = ("workspace-write", "read-only", "danger-full-access")

# The Codex process inherits only these variables from handler's environment.
# Everything else (DATABASE_URL, HANDLER_ADMIN_KEY, storage credentials, ...) is
# withheld, because commands the agent runs can read Codex's environment.
DEFAULT_ENV_ALLOWLIST = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TERM",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
)
# Needed by the Codex process itself to call the model; hidden from the commands
# it runs by the shell environment policy below.
CODEX_CREDENTIAL_ENV = ("CODEX_API_KEY", "OPENAI_API_KEY")
# Glob patterns (case-insensitive in Codex) removed from every command's env.
SHELL_ENV_EXCLUDE = (
    "CODEX_API_KEY", "OPENAI_API_KEY", "*_KEY", "*KEY_*", "*TOKEN*", "*SECRET*",
    "*PASSWORD*", "*DATABASE_URL*", "AWS_*", "BUCKETEER_*",
)


def codex_binary() -> str:
    binary = os.environ.get("HANDLER_CODEX_BIN") or shutil.which("codex")
    if binary:
        return binary
    raise FileNotFoundError("Codex executable not found; set HANDLER_CODEX_BIN or add codex to PATH")


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else json.dumps(value)


def _toml(value: str | list[str] | dict[str, str]) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_toml_key(k)}={_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


def _mcp_args(servers: list[McpServerSpec]) -> list[str]:
    args: list[str] = []
    for server in servers:
        prefix = f"mcp_servers.{_toml_key(server.name)}"
        settings: dict[str, str | list[str] | dict[str, str]] = {}
        # Handler's gateway authorizes every MCP call (grants + approvals), so
        # Codex must not hold calls for a human prompt nobody can answer.
        settings["default_tools_approval_mode"] = "approve"
        if server.url is not None:
            settings["url"] = server.url
            if server.headers:
                settings["http_headers"] = server.headers
        elif server.command:
            settings["command"] = server.command[0]
            settings["args"] = server.command[1:]
            settings["env"] = server.env
        for key, value in settings.items():
            args.extend(["-c", f"{prefix}.{key}={_toml(value)}"])
    return args


def _tool_data(item: dict[str, Any], *, finished: bool) -> dict[str, Any]:
    kind = item["type"]
    tool = "shell" if kind == "command_execution" else kind
    if kind == "mcp_tool_call" and item.get("server") and item.get("tool"):
        tool = f"{item['server']}/{item['tool']}"
    data = {"id": item.get("id"), "tool": tool, "kind": kind}
    if finished:
        data.update(
            output=item.get("aggregated_output", item.get("result")),
            status=item.get("status"),
            exit_code=item.get("exit_code"),
        )
    else:
        data["input"] = (
            item.get("command", "")
            if kind == "command_execution"
            else {k: v for k, v in item.items() if k not in {"id", "type", "status"}}
        )
    return data


def _event(raw: dict[str, Any], model: str | None) -> RunEvent | None:
    event_type = raw.get("type")
    if not isinstance(event_type, str):
        logger.debug("Skipping Codex event with an unknown type: %r", raw)
        return None
    if event_type == "thread.started":
        return RunEvent(RunEventKind.SESSION, {"session_id": raw.get("thread_id")}, raw=raw)
    if event_type == "turn.started":
        return RunEvent(RunEventKind.TURN_STARTED, raw=raw)
    if event_type in {"item.started", "item.completed"}:
        item = raw.get("item")
        if not isinstance(item, dict):
            logger.debug("Skipping Codex event without an item: %r", raw)
            return None
        kind = item.get("type")
        finished = event_type == "item.completed"
        if isinstance(kind, str) and kind in _TOOL_TYPES:
            mapped = RunEventKind.TOOL_FINISHED if finished else RunEventKind.TOOL_STARTED
            return RunEvent(mapped, _tool_data(item, finished=finished), raw=raw)
        if finished and isinstance(kind, str) and kind in {"agent_message", "reasoning"}:
            mapped = RunEventKind.ASSISTANT_TEXT if kind == "agent_message" else RunEventKind.REASONING
            return RunEvent(mapped, {"text": item.get("text", "")}, raw=raw)
    elif event_type == "turn.completed":
        usage = raw.get("usage") or {}
        return RunEvent(
            RunEventKind.USAGE,
            {"model": raw.get("model") or model},
            usage=TokenUsage(
                input_tokens=usage.get("input_tokens", 0),
                cached_input_tokens=usage.get("cached_input_tokens", 0),
                cache_write_tokens=usage.get("cache_write_input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                reasoning_tokens=usage.get("reasoning_output_tokens", 0),
            ),
            raw=raw,
        )
    elif event_type in {"turn.failed", "error"}:
        error = raw.get("error") or raw
        if isinstance(error, str):
            error = {"message": error}
        return RunEvent(
            RunEventKind.ERROR,
            {
                "message": error.get("message", "Codex turn failed"),
                "retryable": bool(error.get("retryable", raw.get("retryable", False))),
            },
            raw=raw,
        )
    logger.debug("Skipping unknown Codex event: %r", raw)
    return None


async def _drain_stderr(stream: asyncio.StreamReader, tail: bytearray) -> None:
    while chunk := await stream.read(_STDERR_BYTES):
        tail.extend(chunk)
        del tail[:-_STDERR_BYTES]


async def _stdout_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    # Tool output can exceed StreamReader.readline's line size limit.
    pending: list[bytes] = []
    while chunk := await stream.read(64 * 1024):
        lines = chunk.split(b"\n")
        pending.append(lines[0])
        if len(lines) == 1:
            continue
        yield b"".join(pending)
        for line in lines[1:-1]:
            yield line
        pending = [lines[-1]]
    if pending and any(pending):
        yield b"".join(pending)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    # The new session makes the child PID its process group ID, including tools.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        # macOS reports EPERM once the group leader has exited.
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await process.wait()


class CodexHarness:
    name = "codex"

    def __init__(
        self,
        binary: str | Path,
        default_model: str | None = None,
        default_reasoning_effort: str | None = None,
        sandbox: str = "workspace-write",
        approval_policy: str = "never",
        auth_json: Path | None = None,
        auth_json_link: bool = False,
        extra_args: list[str] | None = None,
        env_allowlist: tuple[str, ...] | list[str] = DEFAULT_ENV_ALLOWLIST,
    ) -> None:
        if sandbox not in SANDBOX_MODES:
            raise ValueError(f"sandbox must be one of {SANDBOX_MODES}, got {sandbox!r}")
        if sandbox == "danger-full-access":
            logger.warning(
                "Codex sandbox disabled (danger-full-access): agent commands run with the "
                "container's full permissions; only the environment allowlist protects secrets"
            )
        self.binary = str(binary)
        self.default_model = default_model
        self.default_reasoning_effort = default_reasoning_effort
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.auth_json = auth_json
        # Link instead of copy: every run shares one login file, so token
        # refreshes by one Codex process are seen by all (copies would drift
        # and rotated refresh tokens would invalidate each other).
        self.auth_json_link = auth_json_link
        self.extra_args = list(extra_args or [])
        self.env_allowlist = tuple(env_allowlist)
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._catalog: list[dict] | None = None
        self._catalog_retry_at = 0.0
        self._catalog_lock = asyncio.Lock()

    async def defaults(self) -> dict[str, str | None]:
        """Model and effort a run uses when the request sets neither.

        Codex runs with --ignore-user-config, so without -m it takes the first
        listed model of its catalog (lowest priority), and without an effort
        that model's default reasoning level.
        """
        model, effort = self.default_model, self.default_reasoning_effort
        if model is None or effort is None:
            catalog = await self.model_catalog()
            if model is None:
                listed = sorted((m for m in catalog if m.get("visibility") == "list"),
                                key=lambda m: m.get("priority", 1 << 30))
                model = listed[0].get("slug") if listed else None
            if effort is None:
                entry = next((m for m in catalog if m.get("slug") == model), None)
                effort = entry.get("default_reasoning_level") if entry else None
        return {"model": model, "reasoning_effort": effort}

    async def model_catalog(self) -> list[dict]:
        """Codex's model catalog (`codex debug models`), cached; [] if unavailable."""
        async with self._catalog_lock:
            if self._catalog is None and time.monotonic() >= self._catalog_retry_at:
                self._catalog = await self._load_catalog()
                if self._catalog is None:
                    self._catalog_retry_at = time.monotonic() + 300
            return self._catalog or []

    async def _load_catalog(self) -> list[dict] | None:
        with tempfile.TemporaryDirectory(prefix="handler-codex-catalog-") as home:
            if self.auth_json is not None and self.auth_json.exists():
                os.symlink(self.auth_json.resolve(), Path(home) / "auth.json")
            env = {k: os.environ[k] for k in (*self.env_allowlist, *CODEX_CREDENTIAL_ENV)
                   if k in os.environ}
            env.update({"CODEX_HOME": home, "HOME": home})
            # The refreshed catalog is what runs see; the bundled one is the fallback.
            for extra in ([], ["--bundled"]):
                try:
                    process = await asyncio.create_subprocess_exec(
                        self.binary, "debug", "models", *extra, env=env,
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL)
                    try:
                        out, _ = await asyncio.wait_for(process.communicate(), 30)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                        continue
                    if process.returncode == 0:
                        models = json.loads(out).get("models")
                        if isinstance(models, list):
                            return [m for m in models if isinstance(m, dict)]
                except (OSError, ValueError) as exc:
                    logger.warning("codex model catalog unavailable: %s", exc)
        return None

    def _argv(self, req: RunRequest) -> list[str]:
        argv = [
            self.binary, "exec", "--json", "--ignore-user-config", "--skip-git-repo-check",
            "-s", self.sandbox, "-C", str(req.workspace),
            # -i takes several values; the "-c" that follows always ends the list
            *[arg for image in req.images for arg in ("-i", str(image))],
            "-c", f'approval_policy="{self.approval_policy}"',
            "-c", 'shell_environment_policy.inherit="core"',
            "-c", "shell_environment_policy.exclude=" + _toml(list(SHELL_ENV_EXCLUDE)),
        ]
        if model := req.model or self.default_model:
            argv.extend(["-m", model])
        if effort := req.reasoning_effort or self.default_reasoning_effort:
            argv.extend(["-c", f"model_reasoning_effort={effort}"])
        argv.extend(_mcp_args(req.mcp_servers))
        argv.extend(self.extra_args)
        if req.session_id:
            argv.extend(["resume", req.session_id])
        argv.append(req.prompt)
        return argv

    def _prepare(self, req: RunRequest) -> dict[str, str]:
        req.home.mkdir(parents=True, exist_ok=True)
        req.workspace.mkdir(parents=True, exist_ok=True)
        if self.auth_json is not None and self.auth_json_link:
            target, source = req.home / "auth.json", self.auth_json.resolve()
            if not (target.is_symlink() and target.resolve() == source):
                target.unlink(missing_ok=True)
                os.symlink(source, target)
        elif self.auth_json is not None:
            try:
                fd = os.open(req.home / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as target:
                    try:
                        os.fchmod(target.fileno(), 0o600)
                        with self.auth_json.open("rb") as source:
                            shutil.copyfileobj(source, target)
                    except BaseException:
                        (req.home / "auth.json").unlink()
                        raise
        if req.instructions:
            (req.workspace / "AGENTS.md").write_text(req.instructions, encoding="utf-8")
        env = {k: os.environ[k] for k in (*self.env_allowlist, *CODEX_CREDENTIAL_ENV)
               if k in os.environ}
        env.update({"CODEX_HOME": str(req.home), "HOME": str(req.home)})
        env.update(req.env)
        return env

    async def run(self, req: RunRequest) -> AsyncIterator[RunEvent]:
        process = None
        stderr_task = None
        tail = bytearray()
        session_id = req.session_id
        text = ""
        completed = None
        error_emitted = False
        try:
            async with asyncio.timeout(req.limits.timeout_seconds):
                env = self._prepare(req)
                process = await asyncio.create_subprocess_exec(
                    *self._argv(req), env=env, start_new_session=True,
                    # Codex reads stdin when it isn't a TTY; an inherited open pipe
                    # can stall it indefinitely, so stdin is always closed.
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                self._processes[req.run_id] = process
                assert process.stdout is not None and process.stderr is not None
                stderr_task = asyncio.create_task(_drain_stderr(process.stderr, tail))
                async for line in _stdout_lines(process.stdout):
                    try:
                        raw = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        logger.debug("Skipping malformed Codex JSON line: %r", line)
                        continue
                    if not isinstance(raw, dict):
                        logger.debug("Skipping non-object Codex JSON line: %r", raw)
                        continue
                    ev = _event(raw, req.model or self.default_model)
                    if ev is None:
                        continue
                    if ev.kind == RunEventKind.SESSION:
                        session_id = ev.data["session_id"]
                    elif ev.kind == RunEventKind.ASSISTANT_TEXT:
                        text = ev.data["text"]
                    elif ev.kind == RunEventKind.USAGE:
                        completed = raw
                    elif ev.kind == RunEventKind.ERROR:
                        error_emitted = True
                    yield ev
                    if ev.kind == RunEventKind.ERROR:
                        raise HarnessError(ev.data["message"], retryable=ev.data["retryable"])
                await process.wait()
                await stderr_task
                if process.returncode != 0 or completed is None:
                    reason = (
                        f"Codex exited with status {process.returncode}"
                        if process.returncode != 0 else "Codex exited without turn.completed"
                    )
                    raise HarnessError(f"{reason}: {tail.decode('utf-8', errors='replace')}")
        except (TimeoutError, OSError, HarnessError) as exc:
            if isinstance(exc, TimeoutError):
                error = HarnessError(
                    f"Codex timed out after {req.limits.timeout_seconds}s: "
                    f"{tail.decode('utf-8', errors='replace')}", retryable=True,
                )
            elif isinstance(exc, OSError):
                error = HarnessError(f"Could not start Codex: {exc}")
            else:
                error = exc
            if process is not None:
                await _terminate(process)
            if not error_emitted:
                yield RunEvent(RunEventKind.ERROR, {"message": str(error), "retryable": error.retryable})
            raise error
        finally:
            if process is not None:
                await _terminate(process)
                self._processes.pop(req.run_id, None)
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)
        yield RunEvent(RunEventKind.FINAL, {"text": text, "session_id": session_id}, raw=completed)

    async def interrupt(self, run_id: str) -> None:
        if process := self._processes.get(run_id):
            await _terminate(process)
