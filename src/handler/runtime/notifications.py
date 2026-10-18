"""Notifications: store per user, publish live, and emit `notification.created`.

`Notifier.send` is the one entry point for the `notify` tool and for
automatic notifications (background run failures). Delivery to integrations
(e.g. Crontal's bell) happens through the outbox's sinks, never here.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from .bus import BusMessage, RunEventBus
from .sinks import NOTIFICATION_CREATED, DomainEvent, Outbox
from .store.sqlite_memory import Notification
from .types import Run, Thread, ThreadType, new_id, utcnow

logger = logging.getLogger("handler.runtime.notifications")

ALL = "all"


class Notifier:
    def __init__(self, store: Any, bus: RunEventBus, *, outbox: Outbox | None = None,
                 users_provider: Callable[[], list[str]] | None = None,
                 instance: str = "") -> None:
        self.store = store
        self.bus = bus
        self.outbox = outbox
        self.users_provider = users_provider
        self.instance = instance or "main"

    async def send(self, thread_key: str, run_id: str | None, message: str,
                   recipients: list[str] | str) -> tuple[str, list[Notification]]:
        """Store and publish one notification. Returns (notification id, rows).

        `recipients` is a list of user ids or "all". "all" fans out locally to
        the configured users and is passed on to sinks as "all", so an
        integration can resolve it against its own user directory.
        """
        if recipients == ALL:
            if self.users_provider is None:
                raise ValueError("Broadcast needs a users_provider")
            local = list(self.users_provider())
        elif isinstance(recipients, list) and all(isinstance(u, str) for u in recipients):
            local = recipients
            recipients = list(dict.fromkeys(recipients))
        else:
            raise ValueError("to must be a list of user ids or 'all'")
        notification_id = new_id("ntf")
        created_at = utcnow()
        rows = []
        for user in dict.fromkeys(local):
            row = await self.store.add_notification(user, thread_key, run_id, message)
            data = asdict(row)
            data["created_at"] = row.created_at.isoformat()
            self.bus.publish(BusMessage(thread_key, run_id, "notification", data))
            rows.append(row)
        if self.outbox is not None and self.outbox.sinks:
            thread_type, thread_title = await self._thread_info(thread_key)
            await self.outbox.emit(DomainEvent(NOTIFICATION_CREATED, notification_id, {
                "id": notification_id, "instance": self.instance,
                "recipients": recipients, "message": message,
                "thread_key": thread_key, "run_id": run_id,
                "thread_type": thread_type, "thread_title": thread_title,
                "created_at": created_at.isoformat(),
                "notification_ids": [row.id for row in rows],
            }, occurred_at=created_at))
        return notification_id, rows

    async def _thread_info(self, thread_key: str) -> tuple[str | None, str | None]:
        try:
            thread = await self.store.get_thread(thread_key)
            if thread is None:
                return None, None
            meta = (await self.store.thread_metadata([thread_key], None)).get(thread_key, {})
            return str(thread.type), meta.get("title")
        except Exception:
            logger.warning("thread info unavailable for notification", exc_info=True,
                           extra={"thread_key": thread_key})
            return None, None


class FailureNotifier:
    """Tell the acting user and the instance admins when background work dies.

    Called once per terminal failure (no retry left), never for chat threads.
    At most one notification per thread per `cooldown_seconds`, so a thread
    that keeps receiving events and dying does not flood anyone.
    """

    def __init__(self, notifier: Notifier, *, cooldown_seconds: float = 3600.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.notifier = notifier
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._last: dict[str, float] = {}

    async def run_failed(self, thread: Thread, run: Run, acting_as: str | None) -> bool:
        if thread.type == ThreadType.CHAT:
            return False
        now = self.clock()
        last = self._last.get(thread.key)
        if last is not None and now - last < self.cooldown_seconds:
            return False
        recipients = [acting_as] if acting_as else []
        try:
            users = await self.notifier.store.list_users()
        except Exception:
            users = []
        recipients += [u.id for u in users if u.role == "admin" and not u.disabled]
        recipients = list(dict.fromkeys(recipients))
        if not recipients:
            return False
        self._last[thread.key] = now
        _, title = await self.notifier._thread_info(thread.key)
        label = title if title and title != thread.key else thread.key
        error = " ".join((run.error or "unknown error").split())
        if len(error) > 200:
            error = error[:200] + "…"
        message = (f"{str(thread.type).capitalize()} run failed and will not retry: "
                   f"{label} ({thread.key}). {error}")
        await self.notifier.send(thread.key, run.id, message, recipients)
        return True
