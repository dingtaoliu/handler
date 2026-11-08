"""Decode text files; preserve MarkItDown handling for structured text and previews."""

from __future__ import annotations

from pathlib import Path

from .cache import cached_conversion
from .conversion import convert_document, markdown
from .paging import page_text
from .types import ReadOptions, Reading
from .validation import binary_error, check_text

# Preserve the structured-text conversion used before the reader split.
CONVERTED_TEXT_SUFFIXES = {".json", ".jsonl", ".xml"}


def read(path: Path, options: ReadOptions) -> Reading:
    name = options.name_for(path)
    text = (convert_document(path, name, markdown)
            if path.suffix.lower() in CONVERTED_TEXT_SUFFIXES
            else plain_text(path, name))
    return page_text(text, options.offset, options.max_chars, name)


def to_text(path: Path, max_chars: int) -> str:
    # Download previews historically use MarkItDown even for .txt and .md.
    return check_text(cached_conversion(path, markdown), path.name)[:max_chars]


def plain_text(path: Path, name: str) -> str:
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise binary_error(name, len(data))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is None:
            raise binary_error(name, len(data)) from None
        text = str(best)
    return check_text(text, name, len(data))
