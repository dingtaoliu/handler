"""Background poller: one task per instance, polling each enabled mailbox.

All instances on the host share one process-wide semaphore
(`HANDLER_MAIL_CONCURRENCY`, default 2), so however many instances and
mailboxes there are, at most that many IMAP sessions run at once. A failing
mailbox backs off exponentially (up to an hour); a login failure notifies the
mailbox's users once until it recovers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from typing import Any

from .service import MailboxService
from .store import MailStore
from .transport import MailAuthError, MailError

logger = logging.getLogger("handler.runtime.mailbox")

TICK_SECONDS = 5.0
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
                 clock=time.monotonic) -> None:
        self.service = service
        self.mail = mail_store
        self.concurrency = concurrency
        self.notifier = notifier
        self.clock = clock
        self._due: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._kick = asyncio.Event()

    def poll_soon(self, mailbox_id: str | None = None) -> None:
        """Poll a mailbox (or all) at the next tick, e.g. after it was saved."""
        if mailbox_id is None:
            self._due.clear()
        else:
            self._due[mailbox_id] = 0.0
            self._failures.pop(mailbox_id, None)
        self._kick.set()

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception:  # noqa: BLE001 - keep polling
                    logger.exception("mailbox poller tick failed")
                self._kick.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    waiter = asyncio.ensure_future(self._kick.wait())
                    stopper = asyncio.ensure_future(stop.wait())
                    try:
                        await asyncio.wait({waiter, stopper}, timeout=TICK_SECONDS,
                                           return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        waiter.cancel()
                        stopper.cancel()
        finally:
            for task in self._running.values():
                task.cancel()
            await asyncio.gather(*self._running.values(), return_exceptions=True)

    async def tick(self) -> None:
        now = self.clock()
        for mailbox in await self.mail.list_mailboxes():
            if not mailbox.enabled or mailbox.id in self._running:
                continue
            due = self._due.get(mailbox.id)
            if due is None:
                # First sight: stagger mailboxes so a restart doesn't burst.
                due = self._due[mailbox.id] = now + random.uniform(0, 10)
            if due > now:
                continue
            task = asyncio.create_task(self._poll(mailbox), name=f"mailbox:{mailbox.id}")
            self._running[mailbox.id] = task
            task.add_done_callback(lambda _t, key=mailbox.id: self._running.pop(key, None))

    async def wait_idle(self) -> None:
        """Wait for in-flight polls (tests)."""
        while self._running:
            await asyncio.gather(*list(self._running.values()), return_exceptions=True)

    async def _poll(self, mailbox) -> None:
        async with shared_semaphore(self.concurrency):
            started = self.clock()
            try:
                result = await asyncio.wait_for(self.service.poll(mailbox),
                                                timeout=POLL_TIMEOUT_SECONDS)
            except MailAuthError as exc:
                await self._failed(mailbox, "auth_failed", str(exc))
                return
            except (MailError, TimeoutError) as exc:
                await self._failed(mailbox, "error", str(exc) or "timed out")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("mailbox poll crashed", extra={"mailbox": mailbox.id})
                await self._failed(mailbox, "error", f"{type(exc).__name__}: {exc}")
                return
        self._failures.pop(mailbox.id, None)
        pending = any(f.get("pending") for f in result.folders.values())
        # More waiting (a backfill or a burst): continue soon, not after a full interval.
        delay = 5.0 if pending else float(mailbox.poll_seconds)
        self._due[mailbox.id] = self.clock() + delay
        await self.mail.record_sync(mailbox.id, status="active", error=None, synced=True)
        if mailbox.status == "auth_failed":
            logger.info("mailbox login recovered", extra={"mailbox": mailbox.id})
        if result.fetched:
            logger.info("mailbox synced", extra={
                "mailbox": mailbox.id, "fetched": result.fetched, "new": result.new,
                "woke": result.woke, "seconds": round(self.clock() - started, 1)})

    async def _failed(self, mailbox, status: str, error: str) -> None:
        failures = self._failures[mailbox.id] = self._failures.get(mailbox.id, 0) + 1
        delay = min(float(mailbox.poll_seconds) * (2 ** min(failures, 10)), MAX_BACKOFF_SECONDS)
        self._due[mailbox.id] = self.clock() + delay
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
