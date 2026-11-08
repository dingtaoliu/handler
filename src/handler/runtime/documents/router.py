"""Classify a file once and dispatch to its format-specific reader."""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .. import images as image_types
from . import archives, converted, images, pdf, spreadsheets, text
from .types import MAX_READ_CHARS, MAX_TEXT_CHARS, ReadError, ReadOptions, Reading

logger = logging.getLogger(__name__)

SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm", ".xls", ".csv", ".tsv"}
CONVERTED_SUFFIXES = {".docx", ".pptx", ".html", ".htm", ".xhtml", ".epub",
                      ".msg", ".ipynb", ".rss", ".atom"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".bmp",
                  ".tif", ".tiff"}
MARKITDOWN_SUFFIXES = CONVERTED_SUFFIXES | text.CONVERTED_TEXT_SUFFIXES | {".pdf", ".zip"}
DOCUMENT_SUFFIXES = SPREADSHEET_SUFFIXES | MARKITDOWN_SUFFIXES | {".txt", ".md"}
UNSUPPORTED = {".doc": "legacy Word .doc", ".ppt": "legacy PowerPoint .ppt"}


class FileType(StrEnum):
    TEXT = "text"
    CONVERTED = "converted"
    SPREADSHEET = "spreadsheet"
    PDF = "pdf"
    IMAGE = "image"
    ARCHIVE = "archive"


class Reader(Protocol):
    def read(self, path: Path, options: ReadOptions) -> Reading: ...
    def to_text(self, path: Path, max_chars: int) -> str | None: ...


READERS: dict[FileType, Reader] = {
    FileType.TEXT: text,
    FileType.CONVERTED: converted,
    FileType.SPREADSHEET: spreadsheets,
    FileType.PDF: pdf,
    FileType.IMAGE: images,
    FileType.ARCHIVE: archives,
}


def classify_file(path: Path, *, display: str | None = None,
                  sniff_images: bool = True) -> FileType:
    """Check legacy formats first, then image bytes, then extension families.

    Spreadsheet content detection stays in its reader: an .xls export can be
    an actual workbook, HTML or CSV. Unknown extensions take the text reader,
    which rejects binary content rather than returning decoding garbage.
    """
    suffix = path.suffix.lower()
    if suffix in UNSUPPORTED:
        raise ReadError(f"{display or path.name} is a {UNSUPPORTED[suffix]} file, which can't be read. "
                        "Ask for it as .docx/.pptx or PDF.")
    if (sniff_images and image_types.sniff_file(path)) or suffix in IMAGE_SUFFIXES:
        return FileType.IMAGE
    if suffix in SPREADSHEET_SUFFIXES:
        return FileType.SPREADSHEET
    if suffix == ".pdf":
        return FileType.PDF
    if suffix == ".zip":
        return FileType.ARCHIVE
    if suffix in CONVERTED_SUFFIXES:
        return FileType.CONVERTED
    return FileType.TEXT


def read(path: Path, *, display: str | None = None, offset: int = 0,
         sheet: str | None = None, cells: str | None = None, pages: str | None = None,
         as_images: bool = False, max_chars: int = MAX_READ_CHARS) -> Reading:
    """Read a bounded view with the selected reader and continuation instructions."""
    path = Path(path)
    if not path.is_file():
        raise ReadError(f"not a file: {display or path.name}")
    options = ReadOptions(display=display, offset=offset, sheet=sheet, cells=cells,
                          pages=pages, as_images=as_images, max_chars=max_chars)
    file_type = classify_file(path, display=display)
    return READERS[file_type].read(path, options)


def to_text(path: Path, *, max_chars: int = MAX_TEXT_CHARS) -> str | None:
    """Best-effort download preview; retain the existing supported-suffix policy."""
    path = Path(path)
    if path.suffix.lower() not in DOCUMENT_SUFFIXES:
        return None
    try:
        # Previews historically dispatch by extension; interactive reads also sniff images.
        file_type = classify_file(path, sniff_images=False)
        return READERS[file_type].to_text(path, max_chars)
    except Exception:  # noqa: BLE001 - previews must not fail a download
        logger.info("could not convert %s", path.name, exc_info=True)
        return None
