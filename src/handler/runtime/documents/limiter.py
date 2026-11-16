"""The process-wide cap on parsing, with logging: the front of the reader pool.

Every document read, email parse and image downscale goes through
`capped_read`, which runs the function in a reader child process (pool.py)
under one host-wide cap shared fairly by tenants. A read still waiting for a
slot, or still running, is logged every SLOW_READ_SECONDS; each finished read
logs its child's memory.

Settings (environment, read by the host at startup):
- HANDLER_DOC_READ_MAX_CONCURRENT: reader processes (default 2)
- HANDLER_READER_TIMEOUT_SECONDS: a call running longer is killed (default 120)
- HANDLER_READER_MEMORY_MB: RLIMIT_AS per child, Linux only, 0 = off (default 1536)
- HANDLER_READER_MAX_TASKS: calls before a child is replaced (default 50)
- HANDLER_READER_MAX_RESULT_MB: the largest result a child returns (default 64)
- HANDLER_READER_IDLE_SECONDS: an unused child stops after this (default 60)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from ..context import current_run, current_tenant
from ..observability.memory import process_memory
from .pool import PoolSettings, ReaderCall, ReaderFailed, ReaderPool

logger = logging.getLogger("handler.runtime.documents")

T = TypeVar("T")
READ_LIMIT_ENV = "HANDLER_DOC_READ_MAX_CONCURRENT"
DEFAULT_READ_LIMIT = 2
SLOW_READ_SECONDS = 5.0  # log a read still waiting for a slot, or still running, after this
_SETTINGS_ENV = {
    "timeout_seconds": ("HANDLER_READER_TIMEOUT_SECONDS", float),
    "memory_mb": ("HANDLER_READER_MEMORY_MB", int),
    "max_tasks": ("HANDLER_READER_MAX_TASKS", int),
    "max_result_mb": ("HANDLER_READER_MAX_RESULT_MB", int),
    "idle_seconds": ("HANDLER_READER_IDLE_SECONDS", float),
}
_MINIMUM = {"timeout_seconds": 0.001, "memory_mb": 0, "max_tasks": 1, "max_result_mb": 1,
            "idle_seconds": 0.01}

_lock = threading.Lock()
_settings = PoolSettings(size=DEFAULT_READ_LIMIT)
_limit = DEFAULT_READ_LIMIT
_pool: ReaderPool | None = None
_reader_peaks: OrderedDict[str, float] = OrderedDict()  # run id → reader peak RSS (MB)
_MAX_PEAKS = 1000


def configure_read_limit(raw: str | None) -> int:
    """Set the cap from its setting (empty or None: the default); bad values raise."""
    limit = int(raw) if raw else DEFAULT_READ_LIMIT
    if limit < 1:
        raise ValueError(f"{READ_LIMIT_ENV} must be positive, got {limit}")
    _apply(replace(_settings, size=limit))
    return limit


def configure_readers(env: Mapping[str, str]) -> PoolSettings:
    """Every reader setting from the environment; bad values raise."""
    limit = configure_read_limit(env.get(READ_LIMIT_ENV))
    values: dict[str, Any] = {"size": limit}
    defaults = PoolSettings()
    for name, (key, kind) in _SETTINGS_ENV.items():
        raw = (env.get(key) or "").strip()
        value = kind(raw) if raw else getattr(defaults, name)
        if value < _MINIMUM[name]:
            raise ValueError(f"{key} must be at least {_MINIMUM[name]}, got {raw}")
        values[name] = value
    settings = PoolSettings(**values)
    _apply(settings)
    return settings


def _apply(settings: PoolSettings) -> None:
    global _settings, _limit, _pool
    with _lock:
        old = None
        if settings != _settings:
            old, _pool = _pool, None
        _settings, _limit = settings, settings.size
    if old is not None:
        old.close()


def reader_pool() -> ReaderPool:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ReaderPool(_settings)
        return _pool


def shutdown_readers() -> None:
    """Stop the reader processes (blocking; the host calls it at shutdown)."""
    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.close()


def pop_reader_peak(run_id: str) -> float | None:
    """The highest reader-process RSS seen by this run's reads, once."""
    with _lock:
        return _reader_peaks.pop(run_id, None)


def _note_peak(run_id: str, peak: float | None) -> None:
    if not run_id or peak is None:
        return
    with _lock:
        _reader_peaks[run_id] = max(peak, _reader_peaks.get(run_id, 0.0))
        _reader_peaks.move_to_end(run_id)
        while len(_reader_peaks) > _MAX_PEAKS:
            _reader_peaks.popitem(last=False)


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _display(args: tuple, kwargs: Mapping[str, Any]) -> str:
    if kwargs.get("display"):
        return str(kwargs["display"])
    return Path(args[0]).name if args else "the file"


async def capped_read(source: str, function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run `function(path, ...)` in a reader process; `source` names the caller in logs.

    `function` must be importable (it is sent by reference) and its arguments
    and result picklable. A timeout, crash or memory overrun raises
    ReaderFailed (a ReadError) for this call only.
    """
    pool = reader_pool()
    tenant = current_tenant() or ""
    call = ReaderCall(tenant)
    task = asyncio.ensure_future(pool.run(call, function, args, kwargs))
    waited = 0.0
    try:
        while True:
            try:
                # shield: the timeout must not cancel the read; a caller's cancel still does
                outcome = await asyncio.wait_for(asyncio.shield(task), SLOW_READ_SECONDS)
                break
            except TimeoutError:
                waited += SLOW_READ_SECONDS
                logger.warning(
                    "document read still running" if call.running
                    else "document read waiting for a slot",
                    extra={"source": source, "tenant": tenant, "waited_s": waited,
                           "active_reads": pool.active,
                           "tenant_reads": pool.holding(tenant), "limit": _limit})
    except asyncio.CancelledError:
        task.cancel()  # a queued read never starts; a running one's process is killed
        raise
    path = Path(args[0]) if args else None
    memory = outcome.memory
    ctx = current_run()
    _note_peak(ctx.run_id if ctx else "", memory.get("peak_rss_mb"))
    main = process_memory()
    logger.info("document read", extra={
        "source": source, "tenant": tenant,
        "suffix": path.suffix.lower() if path else None,
        "size_bytes": _size(path) if path else None,
        "outcome": "ok" if outcome.status == "ok" else (
            "error" if outcome.status == "error" else outcome.status),
        "active_reads": outcome.active, "queued_ms": outcome.queued_ms,
        "duration_ms": outcome.duration_ms,
        "rss_mb_before": memory.get("rss_mb_before"), "rss_mb_after": memory.get("rss_mb_after"),
        "peak_rss_mb": memory.get("peak_rss_mb"), "swap_mb": memory.get("swap_mb"),
        "reader_pid": memory.get("reader_pid"), "exit_code": outcome.exit_code,
        "main_rss_mb": main["rss_mb"],
    })
    return outcome.value(_display(args, kwargs), pool.settings.timeout_seconds)


async def capped_preview(source: str, path: Path, *, max_chars: int) -> tuple[str | None, str | None]:
    """A download's text preview (`to_text`) and, when the reader failed, why.

    `to_text` itself never raises; a stopped or crashed reader does, and the
    file is already saved, so callers report the error next to it instead.
    """
    from .router import to_text

    try:
        return await capped_read(source, to_text, path, max_chars=max_chars), None
    except ReaderFailed as exc:
        return None, str(exc)


async def capped_parse(source: str, function: Callable[..., T], data: bytes, /,
                       *args: Any, suffix: str = ".eml", **kwargs: Any) -> T:
    """`capped_read` for bytes already in memory (an email): handed over as a file."""
    target = reader_pool().scratch_dir() / f"{uuid.uuid4().hex}{suffix}"
    await asyncio.to_thread(target.write_bytes, data)
    try:
        return await capped_read(source, function, target, *args, **kwargs)
    finally:
        try:
            os.unlink(target)
        except OSError:
            pass


__all__ = ["READ_LIMIT_ENV", "ReaderFailed", "capped_parse", "capped_preview", "capped_read",
           "configure_read_limit", "configure_readers", "pop_reader_peak", "reader_pool",
           "shutdown_readers"]
