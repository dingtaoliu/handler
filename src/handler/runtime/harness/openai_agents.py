"""Native harness: the OpenAI Agents SDK running in-process.

Lighter than Codex (no subprocess, no CLI install) and needs only an OpenAI
API key. A thread's session is an OpenAI Conversation: the conversation id is
the session id, so a turn sends only the new input and resumes server-side
history. Tools are handler's MCP gateway (memory, automations, connectors)
plus OpenAI's hosted web search and file tools confined to the workspace.
read_file goes through `documents`, so it reads office files, PDFs and images
(returned to the model as image input). There is deliberately no shell.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import mimetypes
import os
import re
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
    ToolOutputImage,
    ToolOutputText,
    WebSearchTool,
    function_tool,
    set_tracing_disabled,
)
from agents.exceptions import AgentsException, MaxTurnsExceeded
from agents.mcp import MCPServerStreamableHttp
from openai.types.shared import Reasoning

from .. import documents
from ..images import sniff_file
from ..settings import HARNESS_DEFAULT_MODELS
from ..types import TokenUsage
from .base import HarnessError, RunEvent, RunEventKind, RunRequest

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 40
DEFAULT_MODEL = HARNESS_DEFAULT_MODELS["openai_agents"]
_RETRYABLE = (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
              openai.InternalServerError)

_HOSTED_TOOLS = {"web_search_call", "file_search_call", "code_interpreter_call"}

# Server-side compaction: once a request's rendered context passes this many
# tokens, the Responses API replaces older items with one compaction item, so
# long threads stop resending their whole history. 0 turns it off.
DEFAULT_COMPACT_THRESHOLD = int(os.environ.get("HANDLER_COMPACT_THRESHOLD", "200000"))

# A Conversation holding a tool call without its output (a run killed
# mid-tool) rejects every later request with this 400.
_MISSING_OUTPUT = re.compile(r"no tool output found for (?:function |custom tool )?call", re.I)
# Call item type -> the output item type that answers it.
_CALL_OUTPUTS = {"function_call": "function_call_output",
                 "custom_tool_call": "custom_tool_call_output"}
INTERRUPTED_OUTPUT = "interrupted — no result"

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
    def read_file(path: str, offset: int = 0, sheet: str | None = None,
                  cells: str | None = None, pages: str | None = None,
                  as_images: bool = False) -> Any:
        """Read any workspace file: text, Excel/CSV, Word, PDF, PowerPoint, HTML,
        EPUB, Outlook .msg, ZIP, JSON/XML, and images (which you see as images).

        Output is capped; when more remains, a note at the end says exactly how
        to call read_file for the next part. Spreadsheets list every sheet and
        show cells as Markdown tables with Excel column letters and row numbers.
        A scanned PDF (no text layer) comes back as page images.

        Args:
            path: File path relative to the workspace, e.g. downloads/quote.xlsx.
            offset: Character offset to continue a long text document from.
            sheet: Spreadsheets only: read just this sheet (by name).
            cells: Spreadsheets only: an A1 range such as "A1:H200", "500:900"
                (rows) or "C:F" (columns).
            pages: PDFs only: pages to read, such as "3", "2-5" or "1,4,7-9".
            as_images: PDFs only: show the pages as images (drawings, stamps,
                signatures, layout) instead of their text.
        """
        try:
            target = _confined(workspace, path)
            reading = documents.read(target, display=path, offset=offset, sheet=sheet,
                                     cells=cells, pages=pages, as_images=as_images)
        except (documents.ReadError, ValueError, OSError) as exc:
            return f"error: {exc}"
        if not reading.images:
            return reading.text
        parts: list[Any] = [ToolOutputText(text=reading.text)]
        for image in reading.images:
            data = base64.b64encode(image.data).decode()
            parts.append(ToolOutputImage(image_url=f"data:{image.mime};base64,{data}"))
        return parts

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
        # From the bytes: a name like "image.png" may hold a JPEG or WebP.
        mime = sniff_file(path) or mimetypes.guess_type(path.name)[0] or "image/png"
        data = base64.b64encode(path.read_bytes()).decode()
        content.append({"type": "input_image", "image_url": f"data:{mime};base64,{data}"})
    return [{"role": "user", "content": content}]


def _raw(item: Any) -> dict[str, Any]:
    raw = getattr(item, "raw_item", item)
    if hasattr(raw, "model_dump"):
        return _without_inline_data(raw.model_dump(mode="json", exclude_none=True))
    return _without_inline_data(raw) if isinstance(raw, dict) else {"value": str(raw)}


def _without_inline_data(value: Any) -> Any:
    """Replace base64 data URLs (images read_file showed the model) with a stub,
    so transcripts and run events don't store megabytes of image data."""
    if isinstance(value, str):
        if value.startswith("data:") and len(value) > 256:
            return f"{value.split(',', 1)[0]},… ({len(value):,} chars omitted)"
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {k: _without_inline_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_without_inline_data(v) for v in value]
    return value


def _arguments(raw: dict[str, Any]) -> Any:
    args = raw.get("arguments")
    if isinstance(args, str):
        try:
            return json.loads(args)
        except ValueError:
            return args
    return args


def _usage(usage: Any) -> TokenUsage:
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0
    # Not declared in the SDK's InputTokensDetails yet; the API sends it and the
    # pydantic model keeps it as an extra field.
    written = getattr(details, "cache_write_tokens", 0) or 0
    reasoning = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
    return TokenUsage(input_tokens=usage.input_tokens or 0, cached_input_tokens=cached,
                      cache_write_tokens=written, output_tokens=usage.output_tokens or 0,
                      reasoning_tokens=reasoning)


def _usage_events(usage: Any, model: str, created: bool) -> list[RunEvent]:
    """One USAGE event per model request, so each is priced on its own.

    Per-request entries keep the API's own token details (cache writes) and let
    pricing apply per-request rules such as the long-context tier. The SDK's
    summed usage drops cache writes, so it is only the fallback.
    """
    entries = list(getattr(usage, "request_usage_entries", None) or [])
    if not entries:
        return [RunEvent(RunEventKind.USAGE, {"model": model}, usage=_usage(usage),
                         raw={"requests": usage.requests, "new_conversation": created})]
    return [
        RunEvent(RunEventKind.USAGE, {"model": model, "single_request": True},
                 usage=_usage(entry),
                 raw={"request": i, "requests": len(entries), "new_conversation": created})
        for i, entry in enumerate(entries, 1)
    ]


def _failed_usage(result: Any, model: str, created: bool) -> list[RunEvent]:
    """Usage of a run that raised; empty when it made no model request."""
    usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
    if usage is None or not (getattr(usage, "requests", 0)
                             or getattr(usage, "request_usage_entries", None)):
        return []
    return _usage_events(usage, model, created)


def missing_tool_output(exc: BaseException) -> bool:
    return isinstance(exc, openai.BadRequestError) and bool(_MISSING_OUTPUT.search(str(exc)))


def _compaction_rejected(exc: BaseException) -> bool:
    """A 400 refusing the compaction setting (e.g. a model without it)."""
    message = str(exc).lower()
    return (isinstance(exc, openai.BadRequestError)
            and ("context_management" in message or "compaction" in message))


async def repair_conversation(client: Any, conversation_id: str) -> int:
    """Answer every tool call in the Conversation that has no output.

    Each gets a placeholder output so the next request is accepted. Returns
    how many outputs were added.
    """
    calls: dict[str, str] = {}
    answered: set[str] = set()
    async for item in client.conversations.items.list(conversation_id, limit=100, order="asc"):
        kind, call_id = getattr(item, "type", None), getattr(item, "call_id", None)
        if not call_id:
            continue
        if kind in _CALL_OUTPUTS:
            calls.setdefault(call_id, _CALL_OUTPUTS[kind])
        elif kind in _CALL_OUTPUTS.values():
            answered.add(call_id)
    missing = [{"type": kind, "call_id": call_id, "output": INTERRUPTED_OUTPUT}
               for call_id, kind in calls.items() if call_id not in answered]
    for start in range(0, len(missing), 20):  # the API takes at most 20 items per call
        await client.conversations.items.create(conversation_id,
                                                items=missing[start:start + 20])
    if missing:
        logger.warning("repaired conversation", extra={"conversation_id": conversation_id,
                                                       "outputs_added": len(missing)})
    return len(missing)


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
                 client: openai.AsyncOpenAI | None = None,
                 compact_threshold: int | None = None) -> None:
        self.client = client or openai.AsyncOpenAI(api_key=api_key)
        self.default_model = default_model or DEFAULT_MODEL
        self.default_reasoning_effort = default_reasoning_effort
        threshold = DEFAULT_COMPACT_THRESHOLD if compact_threshold is None else compact_threshold
        # None when off; a model that refuses compaction turns it off for the process.
        self.compact_threshold: int | None = threshold if threshold > 0 else None
        self._runs: dict[str, Any] = {}

    async def defaults(self) -> dict[str, str | None]:
        """Model and effort a run uses when the request sets neither.

        With no effort the request omits reasoning, so the API picks it (None).
        """
        return {"model": self.default_model, "reasoning_effort": self.default_reasoning_effort}

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
                # Past the model's context window, drop the oldest items instead of failing.
                truncation="auto",
                context_management=([{"type": "compaction",
                                      "compact_threshold": self.compact_threshold}]
                                    if self.compact_threshold else None),
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
        created = False
        text = ""
        result: Any = None
        reported = True  # whether `result`'s usage has been emitted
        finished = False
        try:
            async with asyncio.timeout(req.limits.timeout_seconds):
                session_id, created = await self._session(req)
                yield RunEvent(RunEventKind.SESSION, {"session_id": session_id})
                if req.repair_session and not created:
                    # The thread's previous run died mid-turn and may have left
                    # tool calls without outputs in the Conversation.
                    await repair_conversation(self.client, session_id)
                for server in servers:
                    await server.connect()
                    connected.append(server)
                yield RunEvent(RunEventKind.TURN_STARTED)
                for attempt in range(3):
                    result = Runner.run_streamed(
                        self._agent(req, connected), _input(req),
                        max_turns=req.limits.max_turns or DEFAULT_MAX_TURNS,
                        conversation_id=session_id,
                        run_config=RunConfig(tracing_disabled=True,
                                             workflow_name=f"handler:{req.thread_key}"),
                    )
                    reported = False
                    self._runs[req.run_id] = result
                    progressed = False
                    try:
                        async for event in self._events(result):
                            progressed = True
                            if event.kind is RunEventKind.ASSISTANT_TEXT:
                                text = event.data["text"]
                            yield event
                    except openai.BadRequestError as exc:
                        # Retry in place only when the request was refused before
                        # anything happened (so the input was not stored either).
                        if attempt == 2 or progressed or not self._recover(exc, created):
                            raise
                        if not await self._recover_conversation(exc, session_id):
                            raise
                        reported = True
                        for usage_event in _failed_usage(result, model, created):
                            yield usage_event
                        continue
                    break
                reported = True
                for usage_event in _usage_events(result.context_wrapper.usage, model, created):
                    yield usage_event
                final = result.final_output
                if isinstance(final, str) and final:
                    text = final
                finished = True
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
            if result is not None and not reported:
                # Failed runs still made (and paid for) their model calls.
                reported = True
                for usage_event in _failed_usage(result, model, created):
                    yield usage_event
            if missing_tool_output(exc) and session_id and not created:
                # Leave the Conversation usable for the next turn.
                try:
                    await repair_conversation(self.client, session_id)
                except Exception:  # noqa: BLE001
                    logger.warning("conversation repair failed", exc_info=True)
            logger.warning("openai_agents run failed", extra={"error": str(error)})
            yield RunEvent(RunEventKind.ERROR, {"message": str(error), "retryable": error.retryable})
            raise error from exc
        finally:
            if result is not None and not finished:
                # Stop the SDK's background task: left running, it keeps calling
                # tools and writing to the Conversation after handler gave up.
                with contextlib.suppress(Exception):
                    result.cancel()
            self._runs.pop(req.run_id, None)
            for server in connected:
                try:
                    await server.cleanup()
                except Exception:  # noqa: BLE001
                    logger.debug("MCP cleanup failed", exc_info=True)
        yield RunEvent(RunEventKind.FINAL, {"text": text, "session_id": session_id})

    def _recover(self, exc: BaseException, created: bool) -> bool:
        """Whether a refused request can be fixed and sent again."""
        if self.compact_threshold and _compaction_rejected(exc):
            return True
        return missing_tool_output(exc) and not created

    async def _recover_conversation(self, exc: BaseException, session_id: str) -> bool:
        if self.compact_threshold and _compaction_rejected(exc):
            logger.warning("compaction refused; turning it off", extra={"error": str(exc)[:300]})
            self.compact_threshold = None
            return True
        return await repair_conversation(self.client, session_id) > 0

    async def _events(self, result: Any) -> AsyncIterator[RunEvent]:
        """The SDK run's stream as handler RunEvents."""
        # Calls by id, so each output is reported with its call's name and arguments.
        calls: dict[str, tuple[str, Any]] = {}
        async for event in result.stream_events():
            if event.type != "run_item_stream_event":
                continue
            item, raw = event.item, _raw(event.item)
            if event.name == "tool_called":
                call_id = raw.get("call_id") or raw.get("id")
                tool = raw.get("name") or raw.get("type", "tool")
                args = raw.get("action") if "arguments" not in raw else _arguments(raw)
                calls[str(call_id)] = (tool, args)
                yield RunEvent(RunEventKind.TOOL_STARTED, {
                    "id": call_id, "tool": tool, "kind": raw.get("type", "function_call"),
                    "input": args}, raw=raw)
                if raw.get("type") in _HOSTED_TOOLS:
                    # Hosted tools run inside the model call; no output item follows.
                    yield RunEvent(RunEventKind.TOOL_FINISHED, {
                        "id": call_id, "tool": tool, "kind": raw["type"], "input": args,
                        "output": raw.get("action"), "status": raw.get("status")}, raw=raw)
            elif event.name == "tool_output":
                call_id = raw.get("call_id") or raw.get("id")
                tool, args = calls.pop(str(call_id), ("tool", None))
                yield RunEvent(RunEventKind.TOOL_FINISHED, {
                    "id": call_id, "tool": tool, "kind": "function_call_output",
                    "input": args,
                    "output": _without_inline_data(getattr(item, "output", None)),
                    "status": "completed"}, raw=raw)
            elif event.name == "message_output_created":
                message = ItemHelpers.text_message_output(item)
                if message:
                    yield RunEvent(RunEventKind.ASSISTANT_TEXT, {"text": message}, raw=raw)
            elif event.name == "reasoning_item_created":
                summary = " ".join(s.get("text", "") for s in raw.get("summary", [])
                                   if isinstance(s, dict))
                yield RunEvent(RunEventKind.REASONING, {"text": summary}, raw=raw)

    async def interrupt(self, run_id: str) -> None:
        if result := self._runs.get(run_id):
            result.cancel()
