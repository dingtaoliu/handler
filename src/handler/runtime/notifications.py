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
from .types import Run, Thread, ThreadSpec, ThreadType, new_id, utcnow

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
                   recipients: list[str] | str, *,
                   local_only: bool = False) -> tuple[str, list[Notification]]:
        """Store and publish one notification. Returns (notification id, rows).

        `recipients` is a list of user ids or "all". "all" fans out locally to
        the configured users and is passed on to sinks as "all", so an
        integration can resolve it against its own user directory.
        `local_only` keeps it in Handler (no sink delivery): for notices about
        the sinks themselves, which would otherwise feed their own outbox.
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
        if self.outbox is not None and self.outbox.sinks and not local_only:
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
    """Tell people when work stops: a thread parks, or a delivery dead-letters.

    A thread parks when its run fails with no retry left (`threads.dead`). For
    background threads the acting user and the tenant's admins are told. Chat
    sends no notice: the speaker already sees the error in the conversation, and
    their next message retries it. At most one park notice per thread per
    `cooldown_seconds`, so a thread that keeps being retried and parking does not
    flood anyone.
    """

    def __init__(self, notifier: Notifier, *, cooldown_seconds: float = 3600.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.notifier = notifier
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._last: dict[str, float] = {}

    async def _admins(self) -> list[str]:
        try:
            users = await self.notifier.store.list_users()
        except Exception:
            logger.warning("admins unavailable for a notice", exc_info=True)
            return []
        return [u.id for u in users if u.role == "admin" and not u.disabled]

    def _cooling(self, key: str) -> bool:
        now = self.clock()
        last = self._last.get(key)
        if last is not None and now - last < self.cooldown_seconds:
            return True
        self._last[key] = now
        return False

    async def _label(self, thread_key: str) -> str:
        _, title = await self.notifier._thread_info(thread_key)
        return f"{title} ({thread_key})" if title and title != thread_key else thread_key

    async def run_failed(self, thread: Thread, run: Run, acting_as: str | None) -> bool:
        """The thread parked after this run. True if a notice was sent."""
        if thread.type == ThreadType.CHAT:
            return False
        recipients = [acting_as] if acting_as else []
        recipients = list(dict.fromkeys(recipients + await self._admins()))
        if not recipients or self._cooling(thread.key):
            return False
        message = (f"{str(thread.type).capitalize()} thread parked: {await self._label(thread.key)}. "
                   f"Its run failed and will not retry: {_short(run.error)} "
                   "Nothing more runs on it until an admin retries it (Retry in Handler's "
                   "Activity tab or the thread).")
        await self.notifier.send(thread.key, run.id, message, recipients)
        return True

    async def event_on_parked(self, event: Any, thread: ThreadSpec) -> bool:
        """New work arrived on a parked thread; called once per parking."""
        stored = await self.notifier.store.get_thread(thread.key)
        owner = [] if thread.type == ThreadType.CHAT or not thread.owner_user_id \
            else [thread.owner_user_id]
        recipients = list(dict.fromkeys(owner + await self._admins()))
        logger.warning("event queued on parked thread", extra={
            "tenant": self.notifier.instance, "thread_key": thread.key,
            "event_type": event.type, "event_id": event.id})
        if not recipients:
            return False
        error = _short(stored.last_error if stored is not None else None)
        message = (f"New {event.type} waiting on parked thread {await self._label(thread.key)}. "
                   f"It is kept and runs when an admin retries the thread. Last error: {error}")
        await self.notifier.send(thread.key, None, message, recipients)
        return True

    async def delivery_dead(self, sink: str, key: str, error: str) -> bool:
        """A delivery to `sink` dead-lettered. One notice per sink per cooldown.

        Kept in Handler (local_only): a notice through the failing sink would
        only dead-letter in turn.
        """
        recipients = await self._admins()
        if not recipients or self._cooling(f"sink:{sink}"):
            return False
        message = (f"Deliveries to {sink} are failing and one was given up after its last "
                   f"retry ({key}): {_short(error)} Redeliver it from the admin API "
                   f"(POST /api/deliveries/redeliver) once {sink} works again.")
        await self.notifier.send(f"delivery:{sink}", None, message, recipients, local_only=True)
        return True


def _short(error: str | None, limit: int = 200) -> str:
    text = " ".join((error or "unknown error").split())
    return text if len(text) <= limit else text[:limit] + "…"
