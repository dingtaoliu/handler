from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Event:
    type: str  # "user_message", "email_received", "timer_fired", etc.
    source: str  # channel name
    data: dict = field(default_factory=dict)
    conversation_id: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    _response_future: Any = field(default=None, repr=False, compare=False)
