"""Deterministic, typed CSV export shared by every harness."""
from __future__ import annotations

import csv
import hashlib
import io
import math
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

MAX_ROWS = 50_000
MAX_COLUMNS = 100
MAX_BYTES = 10 * 1024 * 1024
MAX_CELL_CHARS = 20_000
TYPES = {"text", "number", "boolean", "date"}


def _cell(value: Any, kind: str) -> tuple[str, bool]:
    if value is None:
        return "", False
    if kind == "text":
        if not isinstance(value, str):
            raise ValueError("text cells must be strings")
        text = value
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("number cells must be finite JSON numbers")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("number cells must be finite")
        return str(value), False
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError("boolean cells must be true or false")
        return "true" if value else "false", False
    else:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError("date cells must be YYYY-MM-DD strings")
        return value, False
    if len(text) > MAX_CELL_CHARS or "\0" in text:
        raise ValueError(f"text cells must be NUL-free and at most {MAX_CELL_CHARS} characters")
    # CSV quoting does not prevent spreadsheet apps interpreting text as formulas.
    # Explicitly expose the count of literalized cells; never evaluate expressions.
    literalized = text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@"))
    return ("'" + text if literalized else text), literalized


def write_csv(workspace: Path, path: str, columns: list[dict], rows: list[dict], *,
              overwrite: bool = False) -> dict:
    root = workspace.resolve()
    target = (root / path).resolve()
    if target == root or root not in target.parents:
        raise ValueError("path escapes the workspace")
    if target.suffix.lower() != ".csv":
        raise ValueError("path must end in .csv")
    if not 1 <= len(columns) <= MAX_COLUMNS or len(rows) > MAX_ROWS:
        raise ValueError(f"use 1–{MAX_COLUMNS} columns and at most {MAX_ROWS} rows")
    keys: set[str] = set()
    headings = []
    for col in columns:
        if set(col) - {"key", "heading", "type"} or not isinstance(col.get("key"), str):
            raise ValueError("columns require key, heading and type")
        key = col["key"]
        if not key or key in keys or col.get("type") not in TYPES:
            raise ValueError("column keys must be nonempty/unique; use text, number, boolean or date")
        heading = col.get("heading")
        if not isinstance(heading, str) or not heading.strip():
            raise ValueError("column headings must be nonempty strings")
        headings.append(_cell(heading, "text"))
        keys.add(key)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([h[0] for h in headings])
    lines = [buffer.getvalue()]
    literalized = sum(h[1] for h in headings)
    encoded_size = 3 + len(buffer.getvalue().encode("utf-8"))
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) - keys:
            raise ValueError(f"row {number}: unknown columns or non-object row")
        try:
            cells = [_cell(row.get(c["key"]), c["type"]) for c in columns]
        except ValueError as exc:
            raise ValueError(f"row {number}: {exc}") from exc
        buffer.seek(0)
        buffer.truncate()
        writer.writerow([c[0] for c in cells])
        line = buffer.getvalue()
        encoded_size += len(line.encode("utf-8"))
        if encoded_size > MAX_BYTES:
            raise ValueError(f"CSV exceeds {MAX_BYTES} bytes; export fewer rows")
        literalized += sum(c[1] for c in cells)
        lines.append(line)
    data = "".join(lines).encode("utf-8-sig")
    if len(data) > MAX_BYTES:
        raise ValueError(f"CSV exceeds {MAX_BYTES} bytes")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Stage the entire validated export; link provides atomic no-clobber creation.
    fd, staging = tempfile.mkstemp(prefix=".csv-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        if overwrite:
            os.replace(staging, target)
        else:
            os.link(staging, target)
    finally:
        Path(staging).unlink(missing_ok=True)
    return {"path": str(target.relative_to(root)), "filename": target.name,
            "content_type": "text/csv", "rows": len(rows), "columns": len(columns),
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "literalized_cells": literalized}
