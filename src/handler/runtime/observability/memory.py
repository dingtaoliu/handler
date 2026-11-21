"""Process memory samples for the structured logs (dyno R14 investigation)."""

from __future__ import annotations

import asyncio
import resource
import sys
from pathlib import Path

_STATUS = Path("/proc/self/status")
_FIELDS = {"VmRSS": "rss_mb", "VmHWM": "peak_rss_mb", "VmSwap": "swap_mb"}


def process_memory() -> dict[str, float | None]:
    """This process's current RSS, peak RSS and swap in MB.

    Peak is the high-water mark since the process started: it catches a spike
    that came and went between two samples. Without /proc (macOS) only the peak
    is known.
    """
    sample: dict[str, float | None] = dict.fromkeys(_FIELDS.values())
    try:
        for line in _STATUS.read_text().splitlines():
            name, _, value = line.partition(":")
            if name in _FIELDS:
                sample[_FIELDS[name]] = round(int(value.split()[0]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        sample["peak_rss_mb"] = round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    return sample


class RunPeak:
    """The main process's highest RSS while one run is going, sampled each second.

    The process's own peak (VmHWM) spans its whole life, so it can't say which
    run a spike belonged to; this can, to the sampling interval. Concurrent runs
    each see the shared process, so a spike shows in every run active then.
    None without /proc (macOS).
    """

    INTERVAL_SECONDS = 1.0

    def __init__(self) -> None:
        self.peak: float | None = None
        self.note()

    def note(self) -> None:
        rss = process_memory()["rss_mb"]
        if rss is not None and (self.peak is None or rss > self.peak):
            self.peak = rss

    async def sample(self) -> None:
        while True:
            await asyncio.sleep(self.INTERVAL_SECONDS)
            self.note()

    def finish(self) -> float | None:
        self.note()
        return self.peak
