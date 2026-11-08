"""Bound text output and explain how to continue reading."""

from __future__ import annotations

from .types import Reading

def page_text(text: str, offset: int, max_chars: int, name: str) -> Reading:
    total = len(text)
    offset = max(0, offset)
    if offset >= total and total:
        return Reading(f"[{name}: offset {offset:,} is past the end ({total:,} characters).]")
    end = min(total, offset + max_chars)
    if end < total:
        # End on a line break when one is near, so a table row isn't split.
        cut = text.rfind("\n", offset + max_chars // 2, end)
        end = cut + 1 if cut > 0 else end
    chunk = text[offset:end]
    if offset or end < total:
        note = f"\n\n[{name}: characters {offset:,}–{end:,} of {total:,}."
        if end < total:
            note += f" Call read_file with offset={end} to read on."
        chunk += note + "]"
    return Reading(chunk or "(empty file)")
