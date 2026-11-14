"""Process-wide cap on in-process document reads, with memory logging.

Decoding a PDF or workbook can take far more memory than the file's size, and
every instance on a dyno shares its memory. The cap is one dedicated thread pool
rather than a semaphore around `asyncio.to_thread`: a timed-out caller stops
waiting but its thread keeps decoding, and a worker stays busy until the read
really ends, so abandoned reads still count against the cap. Queued reads hold
no thread.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

from ..observability.memory import process_memory

logger = logging.getLogger("handler.runtime.documents")

T = TypeVar("T")
READ_LIMIT_ENV = "HANDLER_DOC_READ_MAX_CONCURRENT"
DEFAULT_READ_LIMIT = 2
SLOW_READ_SECONDS = 5.0  # log a read still waiting for a slot, or still running, after this

_lock = threading.Lock()
_limit = DEFAULT_READ_LIMIT
_executor: ThreadPoolExecutor | None = None
_active = 0


def configure_read_limit(raw: str | None) -> int:
    """Set the cap from its setting (empty or None: the default); bad values raise."""
    global _limit, _executor
    limit = int(raw) if raw else DEFAULT_READ_LIMIT
    if limit < 1:
        raise ValueError(f"{READ_LIMIT_ENV} must be positive, got {limit}")
    with _lock:
        if limit != _limit:
            if _executor is not None:
                _executor.shutdown(wait=False)
            _limit, _executor = limit, None
    return limit


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(_limit, thread_name_prefix="document-read")
        return _executor


def _measured(source: str, queued_at: float, function: Callable[..., T],
              args: tuple, kwargs: Mapping[str, Any], started: threading.Event) -> T:
    global _active
    started.set()
    with _lock:
        _active += 1
        active = _active
    started = time.monotonic()
    before = process_memory()
    outcome = "error"
    try:
        result = function(*args, **kwargs)
        outcome = "ok"
        return result
    finally:
        after = process_memory()
        with _lock:
            _active -= 1
        path = Path(args[0])
        logger.info("document read", extra={
            "source": source, "suffix": path.suffix.lower(),
            "size_bytes": _size(path),
            "outcome": outcome, "active_reads": active,
            "queued_ms": round((started - queued_at) * 1000),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "rss_mb_before": before["rss_mb"], "rss_mb_after": after["rss_mb"],
            "peak_rss_mb": after["peak_rss_mb"], "swap_mb": after["swap_mb"],
        })


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


async def capped_read(source: str, function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run `function(path, ...)` on the read pool; `source` names the caller in logs."""
    context = contextvars.copy_context()  # the log line carries the run's ids
    started = threading.Event()
    future = asyncio.get_running_loop().run_in_executor(
        _pool(), context.run, _measured, source, time.monotonic(), function, args, kwargs,
        started)
    waited = 0.0
    while True:
        try:
            # shield: the timeout must not cancel the read; a caller's cancel still does
            return await asyncio.wait_for(asyncio.shield(future), SLOW_READ_SECONDS)
        except TimeoutError:
            waited += SLOW_READ_SECONDS
            with _lock:
                active = _active
            logger.warning(
                "document read still running" if started.is_set()
                else "document read waiting for a slot",
                extra={"source": source, "waited_s": waited, "active_reads": active,
                       "limit": _limit})
        except asyncio.CancelledError:
            future.cancel()  # a read still queued never starts; a running one finishes
            raise
