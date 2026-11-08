"""Cache document conversions by path, size and modification time."""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable

_cache: OrderedDict[tuple, str] = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_ENTRIES = 16


def cached_conversion(path: Path, convert: Callable[[Path], str]) -> str:
    """Conversions kept per (file, size, mtime), so paging doesn't reconvert."""
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
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
