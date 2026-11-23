"""Thread grouping and quoted text for the workspace email reader."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

ConversationKey = tuple[str, str]

_QUOTE_BANNER = re.compile(
    r"^\s*(?:-{2,}\s*(?:original message|forwarded message|原始邮件|转发邮件|轉寄郵件)\s*-*"
    r"|begin forwarded message:)\s*$", re.IGNORECASE)
_FROM = re.compile(r"^\s*(from|发件人|寄件者|寄件人)\s*[:：]", re.IGNORECASE)
_SENT = re.compile(r"^\s*(sent|date|发送时间|發送時間|寄件日期)\s*[:：]", re.IGNORECASE)
_TO = re.compile(r"^\s*(to|收件人)\s*[:：]", re.IGNORECASE)
_SUBJECT = re.compile(r"^\s*(subject|主题|主旨|標題)\s*[:：]", re.IGNORECASE)
_WROTE = re.compile(r"^\s*On\s+.+\bwrote:\s*$", re.IGNORECASE)


def split_quoted_text(body: str) -> tuple[str, str | None]:
    """Separate top-posted replies from quoted history, preserving inline replies."""
    lines = body.splitlines()
    quote_start = len(lines)
    for index, line in enumerate(lines):
        if _QUOTE_BANNER.match(line):
            quote_start = index
            break
        if _FROM.match(line):
            headers = lines[index + 1:index + 9]
            if any(_SENT.match(h) for h in headers) and (
                    any(_TO.match(h) for h in headers) or any(_SUBJECT.match(h) for h in headers)):
                quote_start = index
                break
    # Separate a trailing > block. Unquoted text after a quoted line can be a
    # new inline reply, so leave earlier interleaved lines in the message.
    trailing = quote_start
    while trailing > 0 and (not lines[trailing - 1].strip()
                            or lines[trailing - 1].lstrip().startswith(">")):
        trailing -= 1
    if any(line.lstrip().startswith(">") for line in lines[trailing:quote_start]):
        quote_start = trailing
        if trailing > 0 and _WROTE.match(lines[trailing - 1]):
            quote_start -= 1
    return ("\n".join(lines[:quote_start]).strip(),
            "\n".join(lines[quote_start:]).strip() or None)


def conversation_groups(links: list[dict[str, Any]]) -> dict[ConversationKey, list[ConversationKey]]:
    """Map linked mailbox threads to the same component, including indirect links."""
    parents: dict[ConversationKey, ConversationKey] = {}

    def root(key: ConversationKey) -> ConversationKey:
        while parents.get(key, key) != key:
            key = parents[key]
        return key

    for link in links:
        a = (link["a_account"], link["a_id"])
        b = (link["b_account"], link["b_id"])
        parents.setdefault(a, a)
        parents.setdefault(b, b)
        left, right = sorted((root(a), root(b)))
        parents[right] = left

    groups: dict[ConversationKey, list[ConversationKey]] = defaultdict(list)
    for key in sorted(parents):
        groups[root(key)].append(key)
    return {member: group for group in groups.values() for member in group}
