"""Native harness: the OpenAI Agents SDK running in-process.

Lighter than Codex (no subprocess, no CLI install) and needs only an OpenAI
API key. A thread's session is an OpenAI Conversation: the conversation id is
the session id, so a turn sends only the new input and resumes server-side
history. Tools are handler's MCP gateway (memory, automations, connectors)
plus OpenAI's hosted web search and file tools confined to the workspace.
There is deliberately no shell.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any, AsyncIterator

import openai
from agents import (
    Agent,
    ItemHelpers,
    ModelSettings,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
    WebSearchTool,
    function_tool,
    set_tracing_disabled,
)
from agents.exceptions import AgentsException, MaxTurnsExceeded
from agents.mcp import MCPServerStreamableHttp
from openai.types.shared import Reasoning

from ..types import TokenUsage
from .base import HarnessError, RunEvent, RunEventKind, RunRequest

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 40
MAX_READ_BYTES = 200_000
_RETRYABLE = (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
              openai.InternalServerError)

_HOSTED_TOOLS = {"web_search_call", "file_search_call", "code_interpreter_call"}

# Handler records runs itself; don't also upload SDK traces to OpenAI.
set_tracing_disabled(True)


def _confined(workspace: Path, path: str) -> Path:
    root = workspace.resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes the workspace: {path}")
    return target


def workspace_tools(workspace: Path) -> list:
    """read/write/list files under the run's workspace (e.g. attachments/)."""

    @function_tool
    def list_files(path: str = ".") -> str:
        """List files under a workspace directory (recursive, relative paths)."""
        base = _confined(workspace, path)
        if not base.is_dir():
            return f"not a directory: {path}"
        root = workspace.resolve()
        files = sorted(str(p.relative_to(root)) for p in base.rglob("*") if p.is_file())
        return "\n".join(files[:500]) or "(empty)"

    @function_tool
    def read_file(path: str) -> str:
        """Read a UTF-8 text file from the workspace."""
        target = _confined(workspace, path)
        data = target.read_bytes()[:MAX_READ_BYTES]
        text = data.decode("utf-8", errors="replace")
        if target.stat().st_size > MAX_READ_BYTES:
            text += f"\n… [truncated at {MAX_READ_BYTES} bytes]"
        return text

    @function_tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a UTF-8 text file in the workspace."""
        target = _confined(workspace, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {path}"

    return [list_files, read_file, write_file]


def _input(req: RunRequest) -> str | list[dict[str, Any]]:
    if not req.images:
        return req.prompt
    content: list[dict[str, Any]] = [{"type": "input_text", "text": req.prompt}]
    for path in req.images:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        data = base64.b64encode(path.read_bytes()).decode()
        content.append({"type": "input_image", "image_url": f"data:{mime};base64,{data}"})
    return [{"role": "user", "content": content}]


def _raw(item: Any) -> dict[str, Any]:
    raw = getattr(item, "raw_item", item)
    if hasattr(raw, "model_dump"):
        return raw.model_dump(mode="json", exclude_none=True)
    return raw if isinstance(raw, dict) else {"value": str(raw)}


def _arguments(raw: dict[str, Any]) -> Any:
    args = raw.get("arguments")
    if isinstance(args, str):
        try:
            return json.loads(args)
        except ValueError:
            return args
    return args


def _usage(usage: Any) -> TokenUsage:
    cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
    reasoning = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
    return TokenUsage(input_tokens=usage.input_tokens or 0, cached_input_tokens=cached,
                      output_tokens=usage.output_tokens or 0, reasoning_tokens=reasoning)


def _error(exc: BaseException, session_id: str | None) -> HarnessError:
    if isinstance(exc, HarnessError):
        return exc
    if (isinstance(exc, openai.NotFoundError) and session_id
            and "conv" in str(exc).lower()):
        # Phrased so run.py recognises a lost session and recovers with a recap.
        return HarnessError(f"conversation not found: {exc}")
    if isinstance(exc, _RETRYABLE):
        return HarnessError(f"OpenAI API error: {exc}", retryable=True)
    if isinstance(exc, MaxTurnsExceeded):
        return HarnessError(f"agent exceeded its turn limit: {exc}")
    if isinstance(exc, (openai.OpenAIError, AgentsException)):
        return HarnessError(f"{type(exc).__name__}: {exc}")
    return HarnessError(f"agent run failed: {type(exc).__name__}: {exc}")


class OpenAIAgentsHarness:
    name = "openai_agents"

    def __init__(self, api_key: str, *, default_model: str | None = None,
                 default_reasoning_effort: str | None = None,
                 client: openai.AsyncOpenAI | None = None) -> None:
        self.client = client or openai.AsyncOpenAI(api_key=api_key)
        self.default_model = default_model or "gpt-6.1-sol"
        self.default_reasoning_effort = default_reasoning_effort
        self._runs: dict[str, Any] = {}

    async def _session(self, req: RunRequest) -> tuple[str, bool]:
        if req.session_id:
            return req.session_id, False
        conversation = await self.client.conversations.create(
            metadata={"handler_thread": req.thread_key[:512]})
        return conversation.id, True

    def _agent(self, req: RunRequest, mcp_servers: list) -> Agent:
        model = req.model or self.default_model
        effort = req.reasoning_effort or self.default_reasoning_effort
        return Agent(
            name="handler",
            instructions=req.instructions or None,
            model=OpenAIResponsesModel(model, self.client),
            model_settings=ModelSettings(
                reasoning=Reasoning(effort=effort, summary="auto") if effort else None,
                include_usage=True,
            ),
            tools=[WebSearchTool(), *workspace_tools(req.workspace)],
            mcp_servers=mcp_servers,
        )

    async def run(self, req: RunRequest) -> AsyncIterator[RunEvent]:
        model = req.model or self.default_model
        servers = [
            MCPServerStreamableHttp(
                {"url": s.url, "headers": dict(s.headers)}, name=s.name,
                cache_tools_list=True, client_session_timeout_seconds=60,
            )
            for s in req.mcp_servers if s.url
        ]
        connected: list = []
        session_id: str | None = None
        text = ""
        try:
            async with asyncio.timeout(req.limits.timeout_seconds):
                session_id, created = await self._session(req)
                yield RunEvent(RunEventKind.SESSION, {"session_id": session_id})
                for server in servers:
                    await server.connect()
                    connected.append(server)
                yield RunEvent(RunEventKind.TURN_STARTED)
                result = Runner.run_streamed(
                    self._agent(req, connected), _input(req),
                    max_turns=req.limits.max_turns or DEFAULT_MAX_TURNS,
                    conversation_id=session_id,
                    run_config=RunConfig(tracing_disabled=True,
                                         workflow_name=f"handler:{req.thread_key}"),
                )
                self._runs[req.run_id] = result
                names: dict[str, str] = {}
                async for event in result.stream_events():
                    if event.type != "run_item_stream_event":
                        continue
                    item, raw = event.item, _raw(event.item)
                    if event.name == "tool_called":
                        call_id = raw.get("call_id") or raw.get("id")
                        tool = raw.get("name") or raw.get("type", "tool")
                        names[str(call_id)] = tool
                        yield RunEvent(RunEventKind.TOOL_STARTED, {
                            "id": call_id, "tool": tool, "kind": raw.get("type", "function_call"),
                            "input": _arguments(raw) or raw.get("action")}, raw=raw)
                        if raw.get("type") in _HOSTED_TOOLS:
                            # Hosted tools run inside the model call; no output item follows.
                            yield RunEvent(RunEventKind.TOOL_FINISHED, {
                                "id": call_id, "tool": tool, "kind": raw["type"],
                                "output": raw.get("action"), "status": raw.get("status")}, raw=raw)
                    elif event.name == "tool_output":
                        call_id = raw.get("call_id") or raw.get("id")
                        yield RunEvent(RunEventKind.TOOL_FINISHED, {
                            "id": call_id, "tool": names.get(str(call_id), "tool"),
                            "kind": "function_call_output", "output": getattr(item, "output", None),
                            "status": "completed"}, raw=raw)
                    elif event.name == "message_output_created":
                        message = ItemHelpers.text_message_output(item)
                        if message:
                            text = message
                            yield RunEvent(RunEventKind.ASSISTANT_TEXT, {"text": message}, raw=raw)
                    elif event.name == "reasoning_item_created":
                        summary = " ".join(s.get("text", "") for s in raw.get("summary", [])
                                           if isinstance(s, dict))
                        yield RunEvent(RunEventKind.REASONING, {"text": summary}, raw=raw)
                usage = result.context_wrapper.usage
                yield RunEvent(RunEventKind.USAGE, {"model": model}, usage=_usage(usage),
                               raw={"requests": usage.requests, "new_conversation": created})
                final = result.final_output
                if isinstance(final, str) and final:
                    text = final
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - normalised into HarnessError
            if isinstance(exc, TimeoutError):
                error = HarnessError(f"agent timed out after {req.limits.timeout_seconds}s",
                                     retryable=True)
            elif isinstance(exc, Exception):
                error = _error(exc, req.session_id)
            else:
                raise
            logger.warning("openai_agents run failed", extra={"error": str(error)})
            yield RunEvent(RunEventKind.ERROR, {"message": str(error), "retryable": error.retryable})
            raise error from exc
        finally:
            self._runs.pop(req.run_id, None)
            for server in connected:
                try:
                    await server.cleanup()
                except Exception:  # noqa: BLE001
                    logger.debug("MCP cleanup failed", exc_info=True)
        yield RunEvent(RunEventKind.FINAL, {"text": text, "session_id": session_id})

    async def interrupt(self, run_id: str) -> None:
        if result := self._runs.get(run_id):
            result.cancel()
