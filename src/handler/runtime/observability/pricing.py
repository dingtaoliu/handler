"""Model price table and cost math. Rates are USD per million tokens."""

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
class PriceEntry:
    model: str  # exact name or glob
    effective_from: date
    input_per_mtok: float | None = None
    cached_input_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    output_per_mtok: float | None = None

    @property
    def is_glob(self) -> bool:
        return any(c in self.model for c in "*?[")


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _rate(value: Any) -> float | None:
    return None if value is None else float(value)


class PriceTable:
    def __init__(self, entries: list[PriceEntry]):
        self.entries = list(entries)

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_PRICES_PATH) -> PriceTable:
        doc = yaml.safe_load(Path(path).read_text()) or {}
        rows = doc.get("prices", []) if isinstance(doc, dict) else doc
        return cls(
            [
                PriceEntry(
                    model=str(r["model"]),
                    effective_from=_as_date(r["effective_from"]),
                    input_per_mtok=_rate(r.get("input_per_mtok")),
                    cached_input_per_mtok=_rate(r.get("cached_input_per_mtok")),
                    cache_write_per_mtok=_rate(r.get("cache_write_per_mtok")),
                    output_per_mtok=_rate(r.get("output_per_mtok")),
                )
                for r in rows
            ]
        )

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

    def cost(self, model: str | None, usage: TokenUsage, at: datetime | date) -> float | None:
        """USD cost, or None if unpriced or a needed rate is null.

        Reasoning tokens are part of output_tokens and are not added again.
        """
        if not model:
            return None
        entry = self.lookup(model, at)
        if entry is None:
            return None
        cached = min(usage.cached_input_tokens, usage.input_tokens)
        parts = [
            (usage.input_tokens - cached, entry.input_per_mtok),
            (cached, entry.cached_input_per_mtok),
            (usage.cache_write_tokens, entry.cache_write_per_mtok),
            (usage.output_tokens, entry.output_per_mtok),
        ]
        total = 0.0
        for tokens, rate in parts:
            if tokens == 0:
                continue
            if rate is None:
                return None
            total += tokens * rate / 1_000_000
        return total
