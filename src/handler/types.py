"""Shared data types and helpers used across handler modules.

Event is the unit of work flowing through the system: channels produce them,
Environment consumes them, and responses flow back through the same channel.

Message is the stored chat turn (user or assistant) persisted in EventStore.

Multi-modal helpers (extract_text_content, image_path_to_base64_url,
messages_to_openai) live here to avoid circular imports between agent/ and
tools/ packages.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam
from openai.types.responses import ResponseInputItemParam

logger = logging.getLogger("handler.types")


# ---------------------------------------------------------------------------
# Multi-modal content helpers
# ---------------------------------------------------------------------------


def extract_text_content(content: str | list) -> str:
    """Extract plain text from message content (str or content blocks)."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block.get("type") == "image":
            parts.append(f"[Image: {block.get('path', 'unknown')}]")
    return "\n".join(parts)


def image_path_to_base64_url(path: str, media_type: str = "image/jpeg") -> str:
    """Read an image file and return a data: URL with base64 encoding."""
    data = Path(path).read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{media_type};base64,{b64}"


def messages_to_openai(messages: list[dict]) -> list[ChatCompletionMessageParam]:
    """Convert stored messages to OpenAI Chat Completions multi-modal format.

    Text-only messages pass through unchanged. Messages with content blocks
    get their image references resolved to base64 data URLs.

    Used by OpenAIManualAgent (Chat Completions API).
    """
    result: list[ChatCompletionMessageParam] = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, list):
            blocks = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    blocks.append({"type": "text", "text": block["text"]})
                elif block.get("type") == "image":
                    try:
                        url = image_path_to_base64_url(
                            block["path"], block.get("media_type", "image/jpeg")
                        )
                        blocks.append({"type": "image_url", "image_url": {"url": url}})
                    except Exception as e:
                        logger.warning(f"failed to read image {block.get('path')}: {e}")
                        blocks.append(
                            {
                                "type": "text",
                                "text": f"[Image unavailable: {block.get('path')}]",
                            }
                        )
            result.append(
                cast(
                    ChatCompletionMessageParam, {"role": msg["role"], "content": blocks}
                )
            )
        else:
            result.append(cast(ChatCompletionMessageParam, msg))
    return result


def messages_to_openai_responses(messages: list[dict]) -> list[ResponseInputItemParam]:
    """Convert stored messages to OpenAI Responses API multi-modal format.

    The Responses API uses ``input_text``/``input_image`` content types
    (not ``text``/``image_url`` used by Chat Completions).

    Used by OpenAIAgent (Agents SDK / Runner).
    """
    result: list[ResponseInputItemParam] = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, list):
            blocks = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    blocks.append({"type": "input_text", "text": block["text"]})
                elif block.get("type") == "image":
                    try:
                        url = image_path_to_base64_url(
                            block["path"], block.get("media_type", "image/jpeg")
                        )
                        blocks.append({"type": "input_image", "image_url": url})
                    except Exception as e:
                        logger.warning(f"failed to read image {block.get('path')}: {e}")
                        blocks.append(
                            {
                                "type": "input_text",
                                "text": f"[Image unavailable: {block.get('path')}]",
                            }
                        )
            result.append(
                cast(ResponseInputItemParam, {"role": msg["role"], "content": blocks})
            )
        else:
            result.append(cast(ResponseInputItemParam, msg))
    return result


@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class RunContext:
    """Shared mutable context for the current agent run.

    Passed to Agent and tool factories so tools can read the current
    conversation_id without coupling to Agent instance state.
    """

    def __init__(self) -> None:
        self.conversation_id: str | None = None


@dataclass
class Event:
    type: str  # "user_message", "email_received", "timer_fired", etc.
    source: str  # channel name
    data: dict = field(default_factory=dict)
    conversation_id: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    _response_future: Any = field(default=None, repr=False, compare=False)
