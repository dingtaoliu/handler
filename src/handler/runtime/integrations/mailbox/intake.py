"""Email intake copies in the mailbox store (read-only "intake" account).

Mail accepted at /ingest/email (the Cloudflare Worker for e.g.
contact@crontal.ai) otherwise lives only in its email thread's transcript.
With the mailbox integration enabled, each accepted message is also stored
in `mail_messages` under the synthetic account id `intake`, so mail_search,
mail_read and mail_read_thread find it next to the connected mailboxes.

The intake account has no `mail_accounts` row: the poller never syncs it, the
/api/mailboxes API never lists it, and the mail tools refuse to draft or send
from it (there is no IMAP/SMTP behind it). Attachments are not kept here;
they stay in the intake thread's workspace.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Mailbox

logger = logging.getLogger("handler.runtime.mailbox.intake")

INTAKE_ID = "intake"
INTAKE_LABEL = "Email intake (read-only)"


def intake_mailbox(address: str | None) -> Mailbox:
    """The synthetic, read-only mailbox the intake copies are filed under."""
    address = address or "intake@handler.invalid"
    return Mailbox(label=INTAKE_LABEL, email=address, username=address, imap_host="",
                   imap_port=0, imap_security="none", smtp_host="", smtp_port=0,
                   smtp_security="none", folders=["INBOX"], wake_mode="off", enabled=False,
                   save_sent_copy=False, id=INTAKE_ID, status="read_only")


class IntakeArchive:
    """Files accepted intake mail into the mailbox store (never wakes a run)."""

    def __init__(self, service: Any, default_address: str | None = None) -> None:
        self.service = service
        self.address = default_address

    async def __call__(self, raw: bytes, *, envelope_to: str | None = None,
                       thread_key: str | None = None) -> str:
        if envelope_to and "@" in envelope_to:
            self.address = envelope_to.strip().lower()
        outcome = await self.service.ingest(
            intake_mailbox(self.address), raw, folder="INBOX", uidvalidity=None, uid=None,
            wake=False)
        logger.info("intake mail archived", extra={"outcome": outcome,
                                                   "thread_key": thread_key})
        return outcome
