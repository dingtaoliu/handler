"""Which heavy library family a reader call will load: PyMuPDF or MarkItDown.

A reader holding both is over the default 220 MB RSS cap on the imports alone
(226-257 MB measured), so the pool (pool.py) gives a call that needs the other
family a fresh reader instead of reusing one. Readers report what they actually
loaded after each call; this module only predicts, in the main process, what a
call is about to load. It never raises and reads at most a file's first bytes.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

PYMUPDF = "pymupdf"
MARKITDOWN = "markitdown"
# Modules whose presence in a reader means it holds that family's memory.
MODULES = {PYMUPDF: ("fitz", "pymupdf"), MARKITDOWN: ("markitdown",)}


def loaded() -> list[str]:
    """The families this process has imported (called in the reader after each call)."""
    return [family for family, names in MODULES.items()
            if any(name in sys.modules for name in names)]


def needed(function: Callable[..., Any], args: tuple, kwargs: Mapping[str, Any]) -> str | None:
    """The family `function(*args, **kwargs)` will import, or None for light calls
    (spreadsheets, CSV, plain text, images, email parsing)."""
    try:
        return _needed(function, args, kwargs)
    except Exception:  # noqa: BLE001 - a guess; the reader reports the truth afterwards
        return None


def _needed(function: Callable[..., Any], args: tuple, kwargs: Mapping[str, Any]) -> str | None:
    from . import pdf, router, spreadsheets, text
    from .router import FileType

    if function is pdf.prepare_upload:
        return PYMUPDF
    if function not in (router.read, router.to_text) or not args:
        return None
    path = Path(args[0])
    preview = function is router.to_text
    if preview and path.suffix.lower() not in router.DOCUMENT_SUFFIXES:
        return None
    try:
        kind = router.classify_file(path, sniff_images=not preview)
    except Exception:  # noqa: BLE001 - e.g. a legacy .doc: refused before any import
        return None
    if kind is FileType.PDF:
        return PYMUPDF
    if kind in (FileType.CONVERTED, FileType.ARCHIVE):
        return MARKITDOWN
    if kind is FileType.TEXT:
        # Previews convert even .txt/.md with MarkItDown; reads only structured text.
        if preview or path.suffix.lower() in text.CONVERTED_TEXT_SUFFIXES:
            return MARKITDOWN
        return None
    if kind is FileType.SPREADSHEET:
        # An .xls that is really an HTML table goes through MarkItDown.
        return MARKITDOWN if spreadsheets._spreadsheet_kind(path) == "html" else None
    return None
