"""Model price table and cost math. Rates are USD per million tokens.

Token accounting follows OpenAI's usage object (see `TokenUsage`):
`input_tokens` is the total input and includes both cache reads
(`cached_input_tokens`) and cache writes (`cache_write_tokens`); each input
token is billed at exactly one of the uncached, cached or cache-write rate.
`output_tokens` includes `reasoning_tokens`, which are billed at the output
rate and never added again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

from ..types import TokenUsage

DEFAULT_PRICES_PATH = Path(__file__).with_name("prices.yaml")


@dataclass(frozen=True)
class Rates:
    input_per_mtok: float | None = None
    cached_input_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    output_per_mtok: float | None = None


@dataclass(frozen=True)
class PriceEntry:
    model: str  # exact name or glob
    effective_from: date
    input_per_mtok: float | None = None
    cached_input_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    output_per_mtok: float | None = None
    # Long-context tier: a single request whose input exceeds this many tokens
    # is billed entirely at `long_context` rates.
    long_context_above_tokens: int | None = None
    long_context: Rates | None = None

    @property
    def is_glob(self) -> bool:
        return any(c in self.model for c in "*?[")

    @property
    def rates(self) -> Rates:
        return Rates(self.input_per_mtok, self.cached_input_per_mtok,
                     self.cache_write_per_mtok, self.output_per_mtok)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _rate(value: Any) -> float | None:
    return None if value is None else float(value)


def _rates(row: dict[str, Any]) -> dict[str, float | None]:
    return {
        "input_per_mtok": _rate(row.get("input_per_mtok")),
        "cached_input_per_mtok": _rate(row.get("cached_input_per_mtok")),
        "cache_write_per_mtok": _rate(row.get("cache_write_per_mtok")),
        "output_per_mtok": _rate(row.get("output_per_mtok")),
    }


def _entry(row: dict[str, Any]) -> PriceEntry:
    long = row.get("long_context")
    if long is not None and not isinstance(long, dict):
        raise ValueError(f"long_context of {row['model']!r} must be a mapping")
    threshold = long.get("above_input_tokens") if long else None
    if long and threshold is None:
        raise ValueError(f"long_context of {row['model']!r} needs above_input_tokens")
    return PriceEntry(
        model=str(row["model"]),
        effective_from=_as_date(row["effective_from"]),
        **_rates(row),
        long_context_above_tokens=None if threshold is None else int(threshold),
        long_context=Rates(**_rates(long)) if long else None,
    )


class PriceTable:
    def __init__(self, entries: list[PriceEntry]):
        self.entries = list(entries)

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_PRICES_PATH) -> PriceTable:
        doc = yaml.safe_load(Path(path).read_text()) or {}
        rows = doc.get("prices", []) if isinstance(doc, dict) else doc
        return cls([_entry(r) for r in rows or []])

    def lookup(self, model: str, at: datetime | date) -> PriceEntry | None:
        """Exact match beats glob; among the winners the latest effective_from."""
        day = _as_date(at)
        live = [e for e in self.entries if e.effective_from <= day]
        for matches in (
            [e for e in live if not e.is_glob and e.model == model],
            [e for e in live if e.is_glob and fnmatchcase(model, e.model)],
        ):
            if matches:
                return max(matches, key=lambda e: e.effective_from)
        return None

    def cost(
        self,
        model: str | None,
        usage: TokenUsage,
        at: datetime | date,
        *,
        single_request: bool = False,
    ) -> float | None:
        """USD cost, or None if unpriced or a needed rate is null.

        The long-context tier is a per-request rule, so it only applies when
        `usage` is known to be one model request (`single_request`). Usage
        summed over several requests is billed at the standard rates.
        """
        if not model:
            return None
        entry = self.lookup(model, at)
        if entry is None:
            return None
        rates = entry.rates
        if (single_request and entry.long_context is not None
                and entry.long_context_above_tokens is not None
                and usage.input_tokens > entry.long_context_above_tokens):
            rates = entry.long_context
        cached = min(max(usage.cached_input_tokens, 0), usage.input_tokens)
        written = min(max(usage.cache_write_tokens, 0), usage.input_tokens - cached)
        parts = [
            (usage.input_tokens - cached - written, rates.input_per_mtok),
            (cached, rates.cached_input_per_mtok),
            (written, rates.cache_write_per_mtok),
            (usage.output_tokens, rates.output_per_mtok),
        ]
        total = 0.0
        for tokens, rate in parts:
            if tokens <= 0:
                continue
            if rate is None:
                return None
            total += tokens * rate / 1_000_000
        return total
