"""Caps on what the main process holds outside the reader pool.

Runs share the main interpreter, so one oversized tool result or file load
weighs on every tenant. Parsing happens in reader processes (documents/pool.py);
what still lands in the main process is bounded here:

- HANDLER_TOOL_RESULT_MAX_CHARS (default 200,000): a gateway tool result over
  this is cut, with a marker saying so, before it is audited, stored or sent
  to the model. Tools already budget their output well below it (Crontal JSON
  60,000, mail threads 60,000, extracted text 20,000), so this is a backstop.
- HANDLER_MAIN_FILE_MAX_MB (default 50): a workspace file a tool loads whole
  into memory (an upload to Crontal, an email attachment, a PDF for the model)
  is refused over this size.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

TOOL_RESULT_ENV = "HANDLER_TOOL_RESULT_MAX_CHARS"
FILE_ENV = "HANDLER_MAIN_FILE_MAX_MB"
DEFAULT_TOOL_RESULT_CHARS = 200_000
DEFAULT_FILE_MB = 50

max_tool_result_chars = DEFAULT_TOOL_RESULT_CHARS
max_file_bytes = DEFAULT_FILE_MB * 1024 * 1024


class FileTooLarge(ValueError):
    """A file over the main-process cap; the message is meant for the agent."""


def configure(env: Mapping[str, str]) -> None:
    """Both caps from the environment (empty: the default); bad values raise."""
    global max_tool_result_chars, max_file_bytes
    chars = int(env.get(TOOL_RESULT_ENV) or DEFAULT_TOOL_RESULT_CHARS)
    megabytes = int(env.get(FILE_ENV) or DEFAULT_FILE_MB)
    if chars < 1000 or megabytes < 1:
        raise ValueError(f"{TOOL_RESULT_ENV} must be at least 1000 and {FILE_ENV} at least 1")
    max_tool_result_chars, max_file_bytes = chars, megabytes * 1024 * 1024


def cap_text(text: str) -> str:
    """`text`, or its start plus a marker when it is over the tool result cap."""
    if len(text) <= max_tool_result_chars:
        return text
    return (text[:max_tool_result_chars]
            + f"\n\n[tool result truncated: {max_tool_result_chars:,} of {len(text):,} "
            "characters shown. Ask for less, e.g. fewer fields, rows or pages.]")


def read_bounded(path: Path, name: str | None = None) -> bytes:
    """A file's bytes, or FileTooLarge when it is over the main-process cap."""
    size = Path(path).stat().st_size
    if size > max_file_bytes:
        raise FileTooLarge(f"{name or Path(path).name} is {size / 1048576:.1f} MB, over the "
                           f"{max_file_bytes // 1048576} MB limit for files handled in memory")
    return Path(path).read_bytes()
