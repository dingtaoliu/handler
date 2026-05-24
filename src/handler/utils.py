"""Shared utility functions with no intra-package dependencies."""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("handler.utils")


async def transcribe_audio(path: Path) -> str:
    """Transcribe an audio file using OpenAI Whisper. Returns the transcript text."""
    import openai

    client = openai.AsyncOpenAI()
    with open(path, "rb") as f:
        result = await client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="text",
        )
    logger.info(f"transcribed {path.name}: {str(result)[:100]}")
    return str(result).strip()


def parse_interval(schedule: str) -> timedelta:
    """Parse a simple interval string like '30m', '2h', '1d' into a timedelta."""
    schedule = schedule.strip().lower()
    if schedule.endswith("m"):
        return timedelta(minutes=int(schedule[:-1]))
    elif schedule.endswith("h"):
        return timedelta(hours=int(schedule[:-1]))
    elif schedule.endswith("d"):
        return timedelta(days=int(schedule[:-1]))
    elif schedule.endswith("s"):
        return timedelta(seconds=int(schedule[:-1]))
    raise ValueError(
        f"Invalid schedule format: {schedule!r}. Use e.g. '30m', '2h', '1d'."
    )


def next_run_from_now(schedule: str) -> str:
    """Compute the next run time as a UTC ISO string."""
    delta = parse_interval(schedule)
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%d %H:%M:%S")
