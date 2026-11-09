"""Conservative replacement of one tracked IMAP draft; never expunge unrelated mail."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

from .transport import MailError


def signature(message: EmailMessage) -> str:
    parts = []
    for part in message.walk():
        if not part.is_multipart():
            data = part.get_payload(decode=True)
            if not isinstance(data, bytes):
                data = b""
            if part.get_content_maintype() == "text":
                data = data.replace(b"\r\n", b"\n")
            parts.append([part.get_content_type(), part.get_filename(), hashlib.sha256(data).hexdigest()])
    headers = {name: str(message.get(name) or "") for name in
               ("From", "To", "Cc", "Subject", "In-Reply-To", "References")}
    return hashlib.sha256(json.dumps([headers, parts], sort_keys=True).encode()).hexdigest()


class DraftReplacementError(MailError):
    def __init__(self, message: str, *, appended: bool = False):
        super().__init__(message)
        self.appended = appended


async def replace_copy(service, mailbox, old_message_id: str, expected_signature: str,
                       replacement: EmailMessage) -> str:
    """Find the old draft by identity and verify content before appending/removing its exact UID."""
    if not re.fullmatch(r"<[^<>\s\"]+>", old_message_id):
        raise DraftReplacementError("The draft has no tracked server Message-ID; inspect it manually")
    if not mailbox.drafts_folder:
        raise DraftReplacementError("The mailbox has no Drafts folder")
    try:
        password = await service.password(mailbox)
    except MailError as exc:
        raise DraftReplacementError(str(exc)) from exc

    def update() -> str:
        session = service.transport.open_imap(service.params(mailbox, password, "imap"))
        appended = False
        try:
            if "UIDPLUS" not in session.capabilities:
                raise MailError("Server lacks UIDPLUS; refusing replacement without targeted expunge")
            session.select(mailbox.drafts_folder, readonly=False)
            uids = session.uid_search(f"HEADER Message-ID {old_message_id}")
            if len(uids) != 1:
                raise MailError("Tracked server draft is missing or duplicated; inspect Drafts before editing")
            raw = session.fetch_message(uids[0])
            parsed = BytesParser(policy=policy.default).parsebytes(raw) if raw else None
            if parsed is None or str(parsed.get("Message-ID")) != old_message_id:
                raise MailError("The server draft identity changed; read it again")
            if signature(parsed) != expected_signature:
                raise MailError("The server draft was edited outside Handler; refusing to overwrite it")
            # An APPEND timeout can occur after the server accepted the new copy.
            # From this point, any failure needs reconciliation rather than retry.
            appended = True
            session.append(mailbox.drafts_folder, replacement.as_bytes(), flags="(\\Draft \\Seen)")
            session.delete_uid(uids[0])
            return f"saved to {mailbox.drafts_folder}"
        except Exception as exc:
            raise DraftReplacementError(
                "Replacement may exist or cleanup failed; reconciliation required. Do not retry."
                if appended else str(exc), appended=appended) from exc
        finally:
            try:
                session.logout()
            except Exception:
                pass

    try:
        return await asyncio.to_thread(update)
    except DraftReplacementError:
        raise
    except MailError as exc:
        raise DraftReplacementError(str(exc)) from exc


async def server_drafts(service, mailbox, *, limit: int = 20, offset: int = 0,
                        draft_ref: str | None = None) -> dict:
    """Read current Drafts without setting Seen; references include UIDVALIDITY."""
    if not mailbox.drafts_folder:
        raise MailError("No Drafts folder is configured")
    password = await service.password(mailbox)

    def read() -> dict:
        session = service.transport.open_imap(service.params(mailbox, password, "imap"))
        try:
            status = session.select(mailbox.drafts_folder, readonly=True)
            if draft_ref:
                if not re.fullmatch(r"[0-9]+:[0-9]+", draft_ref):
                    raise MailError("Invalid server draft reference")
                validity, uid = map(int, draft_ref.split(":"))
                if validity != status.uidvalidity:
                    raise MailError("Draft UIDVALIDITY changed; list server drafts again")
                raw = session.fetch_message(uid)
                if not raw:
                    raise MailError("Server draft not found")
                message = BytesParser(policy=policy.default).parsebytes(raw)
                plain = message.get_body(preferencelist=("plain",))
                body = plain.get_content() if plain else ""
                if plain is None and (html := message.get_body(preferencelist=("html",))) is not None:
                    from ...gateway.gmail import html_to_text
                    body = html_to_text(html.get_content())
                return {"draft_ref": draft_ref, "message_id": str(message.get("Message-ID") or ""),
                    "from": str(message.get("From") or ""), "to": str(message.get("To") or ""),
                    "subject": str(message.get("Subject") or ""), "body": body,
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "attachments": [{"filename": p.get_filename(), "content_type": p.get_content_type()}
                                    for p in message.iter_attachments()]}
            uids = sorted(session.uid_search("ALL"), reverse=True)
            drafts = []
            for uid in uids[offset:offset + limit]:
                raw = session.fetch_header(uid)
                if raw:
                    message = BytesParser(policy=policy.default).parsebytes(raw)
                    drafts.append({"draft_ref": f"{status.uidvalidity}:{uid}",
                        "from": str(message.get("From") or ""),
                        "to": str(message.get("To") or ""), "subject": str(message.get("Subject") or "")})
            return {"drafts": drafts, "next_offset": offset + limit if offset + limit < len(uids) else None}
        finally:
            session.logout()

    return await asyncio.to_thread(read)

