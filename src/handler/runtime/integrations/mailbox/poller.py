"""Mailbox polls, run by the host timer: one `mailbox.poll` due row per enabled mailbox.

The integration registers the `mailbox.poll` timer kind (see MailboxIntegration);
each firing polls one mailbox and returns when it is due again. There is no task
per instance or per mailbox. The mailbox's row is written with its account
(MailStore.put_mailbox: kept while enabled, removed when disabled or deleted).

All instances on the host share one process-wide semaphore
(`HANDLER_MAIL_CONCURRENCY`, default 2), so however many instances and
mailboxes there are, at most that many IMAP sessions run at once. A failing
mailbox backs off exponentially (up to an hour); a login failure notifies the
mailbox's users once until it recovers.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any

from ...types import utcnow
from .service import MailboxService
from .store import POLL_KIND, MailStore
from .transport import MailAuthError, MailError

logger = logging.getLogger("handler.runtime.mailbox")

POLL_TIMEOUT_SECONDS = 600.0
MAX_BACKOFF_SECONDS = 3600.0
_SEMAPHORES: dict[int, asyncio.Semaphore] = {}


def shared_semaphore(size: int) -> asyncio.Semaphore:
    """One semaphore per event loop (the host runs every instance on one loop)."""
    loop = asyncio.get_running_loop()
    semaphore = _SEMAPHORES.get(id(loop))
    if semaphore is None:
        semaphore = _SEMAPHORES[id(loop)] = asyncio.Semaphore(max(1, size))
    return semaphore


class MailboxPoller:
    def __init__(self, service: MailboxService, mail_store: MailStore, *,
                 concurrency: int = 2, notifier: Any = None,
                 clock=time.monotonic, wall=utcnow) -> None:
        self.service = service
        self.mail = mail_store
        self.concurrency = concurrency
        self.notifier = notifier
        self.clock = clock
        self.wall = wall
        self._failures: dict[str, int] = {}
        self._running: dict[str, asyncio.Future] = {}

    async def poll_soon(self, mailbox_id: str) -> None:
        """Poll a mailbox at once (e.g. after it was saved): its due row moves to now."""
        self._failures.pop(mailbox_id, None)
        await self.mail._store.set_due(POLL_KIND, mailbox_id, self.wall())

    async def backfill(self) -> None:
        """At start: a row for each enabled mailbox (kept if it has one; a new one is
        staggered over 10 s so a restart doesn't burst), none for the others."""
        mailboxes = await self.mail.list_mailboxes()
        store = self.mail._store
        now = self.wall()
        for mailbox in mailboxes:
            if mailbox.enabled:
                await store.set_due(POLL_KIND, mailbox.id,
                                    now + timedelta(seconds=random.uniform(0, 10)),
                                    keep_existing=True)
        await store.prune_due(POLL_KIND, [m.id for m in mailboxes if m.enabled])

    async def poll_due(self, mailbox_id: str) -> datetime | None:
        """One `mailbox.poll` firing: poll it, return when it is due next (None: no
        longer polled). Its own failures back off here and never raise."""
        mailbox = await self.mail.get_mailbox(mailbox_id)
        if mailbox is None or not mailbox.enabled:
            return None
        done = self._running[mailbox_id] = asyncio.get_running_loop().create_future()
        try:
            delay = await self._poll(mailbox)
        finally:
            self._running.pop(mailbox_id, None)
            done.set_result(None)
        return self.wall() + timedelta(seconds=delay)

    async def wait_idle(self) -> None:
        """Wait for polls under way (tests)."""
        while self._running:
            await asyncio.gather(*list(self._running.values()), return_exceptions=True)

    async def _poll(self, mailbox) -> float:
        async with shared_semaphore(self.concurrency):
            started = self.clock()
            try:
                result = await asyncio.wait_for(self.service.poll(mailbox),
                                                timeout=POLL_TIMEOUT_SECONDS)
            except MailAuthError as exc:
                return await self._failed(mailbox, "auth_failed", str(exc))
            except (MailError, TimeoutError) as exc:
                return await self._failed(mailbox, "error", str(exc) or "timed out")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("mailbox poll crashed", extra={"mailbox": mailbox.id})
                return await self._failed(mailbox, "error", f"{type(exc).__name__}: {exc}")
        self._failures.pop(mailbox.id, None)
        pending = any(f.get("pending") for f in result.folders.values())
        # More waiting (a backfill or a burst): continue soon, not after a full interval.
        delay = 5.0 if pending else float(mailbox.poll_seconds)
        await self.mail.record_sync(mailbox.id, status="active", error=None, synced=True)
        if mailbox.status == "auth_failed":
            logger.info("mailbox login recovered", extra={"mailbox": mailbox.id})
        if result.fetched:
            logger.info("mailbox synced", extra={
                "mailbox": mailbox.id, "fetched": result.fetched, "new": result.new,
                "woke": result.woke, "seconds": round(self.clock() - started, 1)})
        return delay

    async def _failed(self, mailbox, status: str, error: str) -> float:
        failures = self._failures[mailbox.id] = self._failures.get(mailbox.id, 0) + 1
        delay = min(float(mailbox.poll_seconds) * (2 ** min(failures, 10)), MAX_BACKOFF_SECONDS)
        logger.warning("mailbox poll failed", extra={"mailbox": mailbox.id, "status": status,
                                                     "error": error, "retry_in": delay})
        first_auth_failure = status == "auth_failed" and mailbox.status != "auth_failed"
        await self.mail.record_sync(mailbox.id, status=status, error=error, synced=False)
        if first_auth_failure and self.notifier is not None and mailbox.notify_targets:
            try:
                await self.notifier.send(
                    f"mail:{mailbox.id}:status", None,
                    f"Mailbox {mailbox.email}: login failed, so new mail is not being read. "
                    "Update the password (or client authorization code) under Handler "
                    "settings → Mailboxes.", mailbox.notify_targets)
            except Exception:  # noqa: BLE001
                logger.exception("mailbox failure notification failed")
        return delay
