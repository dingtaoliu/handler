"""Shared local MarkItDown conversion and readable conversion errors."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from .cache import cached_conversion
from .types import ReadError
from .validation import check_text

_converter: Any = None
_converter_lock = threading.Lock()


def _markdown_converter() -> Any:
    global _converter
    with _converter_lock:
        if _converter is None:
            from markitdown import MarkItDown

            _converter = MarkItDown()
    return _converter


def markdown(path: Path) -> str:
    return _markdown_converter().convert(str(path)).text_content or ""


def markdown_html(path: Path) -> str:
    from markitdown import StreamInfo

    info = StreamInfo(extension=".html", mimetype="text/html")
    with path.open("rb") as f:
        return _markdown_converter().convert(f, stream_info=info).text_content or ""


def convert_document(path: Path, name: str, convert: Callable[[Path], str]) -> str:
    try:
        text = cached_conversion(path, convert)
    except Exception as exc:  # noqa: BLE001
        raise ReadError(f"could not convert {name}: {type(exc).__name__}: {exc}") from exc
    return check_text(text, name)

