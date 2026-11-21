"""Cache document conversions by path, size and modification time.

In-process by default. Reader processes (pool.py) are configured with a shared
directory instead, so paging through a document doesn't reconvert it whichever
child serves the next page, and a recycled child keeps the cache.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable

_cache: OrderedDict[tuple, str] = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_ENTRIES = 16
_DISK_ENTRIES = 32
_DISK_BYTES = 256 * 1024 * 1024
_dir: Path | None = None


def configure_dir(path: Path | None) -> None:
    """Keep conversions as files under `path` (shared by processes) instead of memory."""
    global _dir
    _dir = Path(path) if path else None
    if _dir is not None:
        _dir.mkdir(parents=True, exist_ok=True)


def cached_conversion(path: Path, convert: Callable[[Path], str]) -> str:
    """Conversions kept per (file, size, mtime), so paging doesn't reconvert."""
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns,
           getattr(convert, "__qualname__", repr(convert)))
    if _dir is not None:
        return _disk_conversion(_dir, key, path, convert)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    text = convert(path)
    with _cache_lock:
        _cache[key] = text
        while len(_cache) > _CACHE_ENTRIES:
            _cache.popitem(last=False)
    return text


def _disk_conversion(root: Path, key: tuple, path: Path, convert: Callable[[Path], str]) -> str:
    entry = root / (hashlib.sha256(repr(key).encode()).hexdigest() + ".txt")
    try:
        text = entry.read_text(encoding="utf-8", errors="surrogatepass")
        os.utime(entry)  # most recently used
        return text
    except OSError:
        pass
    text = convert(path)
    temporary = entry.with_name(f".{entry.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", errors="surrogatepass")
        temporary.replace(entry)
        _prune(root)
    except OSError:
        temporary.unlink(missing_ok=True)
    return text


def _prune(root: Path) -> None:
    entries = []
    for entry in root.glob("*.txt"):
        try:
            stat = entry.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, entry))
    entries.sort(reverse=True)
    kept = total = 0
    for _, size, entry in entries:
        kept, total = kept + 1, total + size
        if kept > _DISK_ENTRIES or total > _DISK_BYTES:
            entry.unlink(missing_ok=True)
