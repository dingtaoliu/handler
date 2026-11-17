"""Fake readers for reader-pool tests. They run in reader child processes, so
they are module-level (sent by reference) and coordinate through files."""

import os
import signal
import sys
import time
import uuid
from pathlib import Path

from handler.runtime.documents import ReadError


def gated(path, gate: str) -> str:
    """Mark a start under <gate>/started, then wait for <gate>/release."""
    gate_dir = Path(gate)
    (gate_dir / "started" / f"{os.getpid()}-{uuid.uuid4().hex}").touch()
    deadline = time.monotonic() + 30
    while not (gate_dir / "release").exists():
        assert time.monotonic() < deadline, "never released"
        time.sleep(0.01)
    return Path(path).name


def hang(path, *args, **kwargs):
    time.sleep(3600)


def crash(path):
    os.kill(os.getpid(), signal.SIGKILL)


def allocate(path, megabytes: int) -> int:
    block = bytearray(megabytes * 1024 * 1024)
    return len(block)


def big_result(path, megabytes: int) -> bytes:
    return os.urandom(megabytes * 1024 * 1024)


def pid(path) -> int:
    return os.getpid()


def text(path) -> str:
    return "text"


def broken(path):
    raise ReadError("corrupt")


def unparseable(path, *args, **kwargs):
    raise ValueError("cannot parse")


def nothing(path, *args, **kwargs):
    return None


def touch_memory(path, megabytes: int) -> int:
    """Hold `megabytes` of resident memory for a while (written, so it counts)."""
    block = bytearray(megabytes * 1024 * 1024)
    for offset in range(0, len(block), 4096):
        block[offset] = 1
    time.sleep(1)
    return megabytes


def pdf_text_modules(path) -> tuple[str | None, str, list[str]]:
    """Preview and read a PDF, then report which converters the child imported."""
    from handler.runtime import documents

    preview = documents.to_text(path)
    reading = documents.read(path, max_chars=2000)
    return preview, reading.text, sorted(m for m in ("markitdown", "pdfminer", "fitz")
                                         if m in sys.modules)
