"""Read Word, slides, HTML, email and other MarkItDown documents."""

from __future__ import annotations

from pathlib import Path

from .cache import cached_conversion
from .conversion import convert_document, markdown
from .paging import page_text
from .types import ReadOptions, Reading
from .validation import check_text

def read(path: Path, options: ReadOptions) -> Reading:
    name = options.name_for(path)
    text = convert_document(path, name, markdown)
    return page_text(text, options.offset, options.max_chars, name)


def to_text(path: Path, max_chars: int) -> str:
    return check_text(cached_conversion(path, markdown), path.name)[:max_chars]
