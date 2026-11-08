"""Shared reader options, results and output limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MAX_READ_CHARS = 20_000
MAX_TEXT_CHARS = 2_000_000


class ReadError(Exception):
    """A file the reader can't open; the message is meant for the agent."""


@dataclass
class Image:
    mime: str
    data: bytes
    label: str = ""


@dataclass
class Reading:
    text: str
    images: list[Image] = field(default_factory=list)


@dataclass(frozen=True)
class ReadOptions:
    display: str | None = None
    offset: int = 0
    sheet: str | None = None
    cells: str | None = None
    pages: str | None = None
    as_images: bool = False
    max_chars: int = MAX_READ_CHARS

    def name_for(self, path: Path) -> str:
        return self.display or path.name
