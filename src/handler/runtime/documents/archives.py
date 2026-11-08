"""Read ZIP members through the existing MarkItDown archive converter."""

from __future__ import annotations

from pathlib import Path

from . import converted
from .types import ReadOptions, Reading

def read(path: Path, options: ReadOptions) -> Reading:
    return converted.read(path, options)


def to_text(path: Path, max_chars: int) -> str:
    return converted.to_text(path, max_chars)
