"""JSON-lines logging that tags records with the current run context."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from ..context import current_run

# Attributes every LogRecord has; anything else came from `extra=` and is logged.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = current_run()
        if ctx is not None:
            out["run_id"] = ctx.run_id
            out["thread_key"] = ctx.thread_key
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and key not in out:
                out[key] = value
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def configure_json_logging(
    level: str = "INFO", stream: IO[str] | None = None, file: Path | None = None
) -> None:
    """Log JSON lines to `stream`, and also to a rotating `file` (10 MB x 5)."""
    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stderr)]
    if file is not None:
        file = Path(file)
        file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"))
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h.formatter, JsonFormatter):
            root.removeHandler(h)
            h.close()
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(level.upper())
