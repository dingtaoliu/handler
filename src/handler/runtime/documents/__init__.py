"""Shared file reader: route files to text, document, spreadsheet, PDF or image readers.

The public read/to_text API is shared by both harnesses and attachment previews.
Start in router.py for dispatch; each reader owns its format-specific behavior.
"""

from .cli import main
from .images import IMAGE_MAX_BYTES, IMAGE_MAX_SIDE
from .pdf import MAX_PAGE_IMAGES
from .router import (
    DOCUMENT_SUFFIXES,
    IMAGE_SUFFIXES,
    MARKITDOWN_SUFFIXES,
    SPREADSHEET_SUFFIXES,
    read,
    to_text,
)
from .types import MAX_READ_CHARS, MAX_TEXT_CHARS, Image, ReadError, ReadOptions, Reading

__all__ = [
    "read", "to_text", "main", "ReadError", "ReadOptions", "Reading", "Image",
    "MAX_READ_CHARS", "MAX_TEXT_CHARS", "MAX_PAGE_IMAGES", "IMAGE_MAX_SIDE",
    "IMAGE_MAX_BYTES", "DOCUMENT_SUFFIXES", "SPREADSHEET_SUFFIXES",
    "IMAGE_SUFFIXES", "MARKITDOWN_SUFFIXES",
]
