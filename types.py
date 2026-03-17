"""Shared data types used across handler modules.

Event is the unit of work flowing through the system: channels produce them,
Environment consumes them, and responses flow back through the same channel.

Message is the stored chat turn (user or assistant) persisted in EventStore.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class RunContext:
    """Shared mutable context for the current agent run.

    Passed to Agent and tool factories so tools can read the current
    conversation_id without coupling to Agent instance state.
    """

    def __init__(self) -> None:
        self.conversation_id: str | None = None


@dataclass
class Event:
    type: str  # "user_message", "email_received", "timer_fired", etc.
    source: str  # channel name
    data: dict = field(default_factory=dict)
    conversation_id: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    _response_future: Any = field(default=None, repr=False, compare=False)
