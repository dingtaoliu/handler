"""Reject binary or undecodable content before it reaches the model."""

from __future__ import annotations

from .types import ReadError

GARBAGE_RATIO = 0.05


def binary_error(name: str, size: int) -> ReadError:
    return ReadError(f"{name} is a binary file ({size:,} bytes) of a type that can't be "
                     "read as text; nothing was shown. If it is a document, ask for it as "
                     ".xlsx, .docx, .pptx or PDF.")


def check_text(text: str, name: str, size: int | None = None) -> str:
    """Refuse text that is mostly decoding garbage (U+FFFD, NULs, control bytes)."""
    sample = text[:200_000]
    if not sample:
        return text
    bad = sum(1 for ch in sample
              if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t\n\r\f"))
    if bad / len(sample) > GARBAGE_RATIO:
        raise binary_error(name, size if size is not None else len(text))
    return text

