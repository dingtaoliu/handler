"""Mailbox sync, ingest, threading, send and connection tests.

Sync (per mailbox, per folder; the server has no IDLE, so we poll):
- Folder state is (UIDVALIDITY, last_uid, backfill_until_uid). On first sight
  of a folder (or a new UIDVALIDITY) we note the current top UID: everything
  at or below it is *backfill* (only the last `backfill_days`, never wakes the
  agent); everything above it is live mail.
- A UIDVALIDITY change means the server renumbered the folder. We restart
  the backfill from the new numbering; messages we already have (by
  Message-ID) are only re-pointed at their new UID. Unknown messages whose
  INTERNALDATE is after the last successful sync still wake the agent, so
  nothing that arrived during the renumbering is missed.
- Each poll handles at most `MAX_MESSAGES_PER_POLL` messages, one at a time
  (bounded memory); the rest follow on the next poll.

Threading: Message-ID / In-Reply-To / References through `mail_refs` (works
in any arrival order), then a normalised subject among recent conversations
that share a participant (only for reply-looking subjects or messages whose
parents we never saw), else a new conversation keyed on the root id.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Callable

from ...gateway.secret_box import EncryptedSecrets, SecretUnreadable
from ...run import thread_slug
from ...settings import resolve_harness
from ...types import Event, Lane, ThreadSpec, ThreadType, new_id, utcnow
from .config import Mailbox, MailboxTest
from .events import EVENT_TYPE, THREAD_TYPE
from .message import MailMessage, normalize_subject, is_reply_subject, parse_message
from .store import MailStore
from .transport import (
    CONNECT_TIMEOUT_SECONDS,
    FolderInfo,
    MailAuthError,
    MailError,
    MailTransport,
    ServerParams,
    redact,
)

logger = logging.getLogger("handler.runtime.mailbox")

MAX_MESSAGES_PER_POLL = 100
DEFAULT_MAX_MESSAGE_BYTES = 25 * 1024 * 1024
MAX_EVENT_BODY_CHARS = 20_000
SUBJECT_WINDOW_DAYS = 60
MAX_PARTICIPANTS = 50
DEFAULT_MAX_WAKES_PER_HOUR = 30
TEST_TIMEOUT_SECONDS = 12.0
SENT_NAMES = ("sent", "sent messages", "sent items", "sent mail", "已发送", "已發送",
              "已发送邮件", "寄件備份", "发件箱")
DRAFT_NAMES = ("drafts", "draft", "草稿箱", "草稿", "草稿夹")


def thread_key(mailbox_id: str, conversation_id: str) -> str:
    return f"{THREAD_TYPE}:{mailbox_id}:{conversation_id}"


def imap_date(when: datetime) -> str:
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
              "Dec")
    return f"{when.day:02d}-{months[when.month - 1]}-{when.year}"


def special_folder(folders: list[FolderInfo], flag: str, names: tuple[str, ...]) -> str | None:
    for info in folders:
        if flag.lower() in (f.lower() for f in info.flags):
            return info.name
    for info in folders:
        leaf = info.name.rsplit(info.delimiter or "/", 1)[-1] if info.delimiter else info.name
        if leaf.strip().lower() in names:
            return info.name
    return None


def conversation_id_for(mailbox_id: str, root: str) -> str:
    return "c" + hashlib.sha256(f"{mailbox_id}\0{root}".encode()).hexdigest()[:20]


def _addr_list(addresses) -> list[dict]:
    return [a.as_dict() for a in addresses]


@dataclass
class PollResult:
    fetched: int = 0
    new: int = 0
    woke: int = 0
    duplicates: int = 0
    folders: dict[str, dict] = field(default_factory=dict)


class MailboxService:
    def __init__(self, *, store: Any, mail_store: MailStore, secrets: EncryptedSecrets,
                 transport: MailTransport, settings: Any = None, workspace_sync: Any = None,
                 workspaces_dir: Path | None = None, notifier: Any = None,
                 instance_name: str = "", max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
                 max_wakes_per_hour: int = DEFAULT_MAX_WAKES_PER_HOUR,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.mail = mail_store
        self.secrets = secrets
        self.transport = transport
        self.settings = settings
        self.workspace_sync = workspace_sync
        self.workspaces_dir = workspaces_dir
        self.notifier = notifier
        self.instance_name = instance_name
        self.max_message_bytes = max_message_bytes
        self.max_wakes_per_hour = max_wakes_per_hour
        self.clock = clock
        self._wakes: dict[str, deque[float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── credentials ────────────────────────────────────────────────────────

    async def set_password(self, mailbox: Mailbox, password: str) -> None:
        mailbox.secret_ref = mailbox.secret_ref or f"mailbox:{mailbox.id}:password"
        await self.secrets.put(mailbox.secret_ref, password)

    async def password(self, mailbox: Mailbox) -> str:
        try:
            value = await self.secrets.get(mailbox.secret_ref) if mailbox.secret_ref else None
        except SecretUnreadable:
            raise MailAuthError("the stored password cannot be decrypted (the instance's "
                                "secret key changed); enter the password again") from None
        if not value:
            raise MailAuthError("no password is stored for this mailbox")
        return value

    @staticmethod
    def params(source: Mailbox | MailboxTest, password: str, which: str, *,
               timeout: float = CONNECT_TIMEOUT_SECONDS) -> ServerParams:
        username = getattr(source, "username", None) or source.email
        return ServerParams(
            host=getattr(source, f"{which}_host"), port=int(getattr(source, f"{which}_port")),
            security=getattr(source, f"{which}_security"), username=username,
            password=password, allow_self_signed=bool(source.allow_self_signed),
            timeout=timeout)

    async def delete(self, mailbox: Mailbox) -> None:
        if mailbox.secret_ref:
            await self.secrets.delete(mailbox.secret_ref)
        await self.mail.delete_mailbox(mailbox.id)

    # ── connection test ────────────────────────────────────────────────────

    async def test(self, source: Mailbox | MailboxTest, password: str | None = None) -> dict:
        """Log in to IMAP (list folders, examine INBOX) and SMTP (EHLO, TLS, AUTH;
        nothing is sent). Never raises; the result says what failed."""
        if password is None:
            try:
                password = await self.password(source)  # type: ignore[arg-type]
            except MailError as exc:
                return {"ok": False, "imap": {"ok": False, "error": str(exc)},
                        "smtp": {"ok": False, "error": str(exc)}}
        imap: dict[str, Any] = {"ok": False}
        smtp: dict[str, Any] = {"ok": False}

        async def check_imap() -> None:
            try:
                session = await asyncio.to_thread(
                    self.transport.open_imap,
                    self.params(source, password, "imap", timeout=TEST_TIMEOUT_SECONDS))
                try:
                    folders = await asyncio.to_thread(session.list_folders)
                    inbox = await asyncio.to_thread(session.select, "INBOX", readonly=True)
                    imap.update(ok=True, folders=[f.name for f in folders][:100],
                                sent_folder=special_folder(folders, "\\Sent", SENT_NAMES),
                                drafts_folder=special_folder(folders, "\\Drafts", DRAFT_NAMES),
                                inbox_messages=inbox.exists,
                                idle="IDLE" in session.capabilities,
                                capabilities=sorted(session.capabilities))
                finally:
                    await asyncio.to_thread(session.logout)
            except MailError as exc:
                imap["error"] = redact(str(exc), password)
            except Exception as exc:  # noqa: BLE001
                imap["error"] = redact(f"{type(exc).__name__}: {exc}", password)

        async def check_smtp() -> None:
            try:
                client = await asyncio.to_thread(
                    self.transport.open_smtp,
                    self.params(source, password, "smtp", timeout=TEST_TIMEOUT_SECONDS))
                smtp.update(ok=True, auth=list(client.auth_mechanisms))
                await asyncio.to_thread(client.close)
            except MailError as exc:
                smtp["error"] = redact(str(exc), password)
            except Exception as exc:  # noqa: BLE001
                smtp["error"] = redact(f"{type(exc).__name__}: {exc}", password)

        # Both at once with short timeouts: the answer must beat Heroku's 30 s
        # router timeout when Crontal proxies the test.
        await asyncio.gather(check_imap(), check_smtp())
        return {"ok": imap["ok"] and smtp["ok"], "imap": imap, "smtp": smtp}

    # ── polling ────────────────────────────────────────────────────────────

    def lock(self, mailbox_id: str) -> asyncio.Lock:
        return self._locks.setdefault(mailbox_id, asyncio.Lock())

    async def poll(self, mailbox: Mailbox) -> PollResult:
        """One sync of every configured folder. Raises MailError on failure."""
        async with self.lock(mailbox.id):
            password = await self.password(mailbox)
            session = await asyncio.to_thread(
                self.transport.open_imap, self.params(mailbox, password, "imap"))
            result = PollResult()
            try:
                if mailbox.sent_folder is None or mailbox.drafts_folder is None:
                    await self._detect_folders(mailbox, session)
                budget = MAX_MESSAGES_PER_POLL
                for folder in mailbox.folders:
                    budget -= await self._sync_folder(mailbox, session, folder, result, budget)
                    if budget <= 0:
                        break
            finally:
                await asyncio.to_thread(session.logout)
            return result

    async def _detect_folders(self, mailbox: Mailbox, session) -> None:
        try:
            folders = await asyncio.to_thread(session.list_folders)
        except MailError:
            return
        sent = special_folder(folders, "\\Sent", SENT_NAMES)
        drafts = special_folder(folders, "\\Drafts", DRAFT_NAMES)
        current = await self.mail.get_mailbox(mailbox.id) or mailbox
        current.sent_folder = current.sent_folder or sent or ""
        current.drafts_folder = current.drafts_folder or drafts or ""
        await self.mail.put_mailbox(current)
        mailbox.sent_folder, mailbox.drafts_folder = current.sent_folder, current.drafts_folder

    async def _sync_folder(self, mailbox: Mailbox, session, folder: str, result: PollResult,
                           budget: int) -> int:
        status = await asyncio.to_thread(session.select, folder, readonly=True)
        state = await self.mail.get_folder(mailbox.id, folder)
        now = self.clock()
        if state is None or int(state["uidvalidity"] or -1) != status.uidvalidity:
            if status.uidnext:
                top = status.uidnext - 1
            else:
                every = await asyncio.to_thread(session.uid_search, "ALL")
                top = max(every, default=0)
            wake_after = None
            if state is not None:
                wake_after = state.get("synced_at")
                logger.warning("mailbox folder renumbered (UIDVALIDITY changed)", extra={
                    "mailbox": mailbox.id, "folder": folder, "old": state["uidvalidity"],
                    "new": status.uidvalidity})
            since = (now - timedelta(days=mailbox.backfill_days)).isoformat()
            await self.mail.put_folder(mailbox.id, folder, uidvalidity=status.uidvalidity,
                                       last_uid=0, backfill_until_uid=top,
                                       backfill_since=since, wake_after=wake_after)
            state = await self.mail.get_folder(mailbox.id, folder)
        last_uid = int(state["last_uid"] or 0)
        backfill_top = int(state["backfill_until_uid"] or 0)
        candidates: list[int] = []
        if last_uid < backfill_top and mailbox.backfill_days > 0:
            since = datetime.fromisoformat(state["backfill_since"]) if state.get(
                "backfill_since") else now - timedelta(days=mailbox.backfill_days)
            found = await asyncio.to_thread(
                session.uid_search, f"UID {last_uid + 1}:{backfill_top} SINCE {imap_date(since)}")
            candidates += [u for u in found if last_uid < u <= backfill_top]
        floor = max(last_uid, backfill_top)
        found = await asyncio.to_thread(session.uid_search, f"UID {floor + 1}:*")
        candidates += [u for u in found if u > floor]  # "n:*" always returns the top UID
        candidates = sorted(set(candidates))
        stats = result.folders.setdefault(folder, {"pending": 0, "processed": 0})
        if not candidates:
            if last_uid < backfill_top:
                await self.mail.advance_folder(mailbox.id, folder, backfill_top)
            stats["pending"] = 0
            return 0
        batch = candidates[:max(budget, 0)]
        stats["pending"] = len(candidates) - len(batch)
        wake_after = state.get("wake_after")
        wake_after_dt = datetime.fromisoformat(wake_after) if wake_after else None
        metas = await asyncio.to_thread(session.fetch_meta, batch) if batch else []
        for meta in metas:
            backfill = meta.uid <= backfill_top
            wake = not backfill or bool(
                wake_after_dt and meta.internaldate and meta.internaldate > wake_after_dt)
            too_big = meta.size > self.max_message_bytes
            raw = await asyncio.to_thread(
                session.fetch_header if too_big else session.fetch_message, meta.uid)
            result.fetched += 1
            if raw:
                outcome = await self.ingest(
                    mailbox, raw, folder=folder, uidvalidity=status.uidvalidity, uid=meta.uid,
                    internaldate=meta.internaldate, wake=wake, headers_only=too_big,
                    size=meta.size)
                if outcome == "duplicate":
                    result.duplicates += 1
                else:
                    result.new += 1
                    result.woke += outcome == "woke"
            await self.mail.advance_folder(mailbox.id, folder, meta.uid)
            stats["processed"] += 1
        processed_all = len(batch) == len(candidates)
        if processed_all and last_uid < backfill_top:
            await self.mail.advance_folder(mailbox.id, folder, backfill_top)
        elif batch:
            # UIDs the server listed but no longer returns (expunged meanwhile).
            await self.mail.advance_folder(mailbox.id, folder, batch[-1])
        return len(batch)

    # ── ingest ─────────────────────────────────────────────────────────────

    def own_addresses(self, mailbox: Mailbox) -> set[str]:
        return {a.lower() for a in (mailbox.email, mailbox.username) if a and "@" in a}

    async def _conversation(self, mailbox: Mailbox, parsed: MailMessage,
                            participants: list[str]) -> tuple[str, list[str]]:
        """(conversation id, its participants after adding this message's)."""
        ids = [parsed.message_id, *parsed.parent_ids]
        found = await self.mail.conversation_for_refs(mailbox.id, ids)
        norm = normalize_subject(parsed.subject)
        if found is None and norm and (is_reply_subject(parsed.subject) or parsed.parent_ids):
            when = parsed.date_iso or self.clock().isoformat()
            since = (datetime.fromisoformat(when) - timedelta(days=SUBJECT_WINDOW_DAYS))
            own = self.own_addresses(mailbox)
            mine = set(participants) - own
            for conv in await self.mail.conversations_by_subject(
                    mailbox.id, norm, since.isoformat()):
                if mine & (set(conv.get("participants") or []) - own):
                    found = conv["id"]
                    break
        if found is None:
            root = (parsed.references or parsed.in_reply_to or [parsed.message_id])[0]
            found = conversation_id_for(mailbox.id, root)
        existing = await self.mail.get_conversation(mailbox.id, found)
        known = (existing or {}).get("participants") or []
        merged = list(dict.fromkeys([*known, *participants]))[:MAX_PARTICIPANTS]
        return found, merged

    def _wake_allowed(self, mailbox: Mailbox) -> bool:
        window = self._wakes.setdefault(mailbox.id, deque())
        now = time.monotonic()
        while window and now - window[0] > 3600:
            window.popleft()
        if len(window) >= self.max_wakes_per_hour:
            return False
        window.append(now)
        return True

    async def ingest(self, mailbox: Mailbox, raw: bytes, *, folder: str, uidvalidity: int | None,
                     uid: int | None, internaldate: datetime | None = None, wake: bool,
                     headers_only: bool = False, size: int | None = None) -> str:
        """Store one message. Returns "duplicate", "stored" or "woke"."""
        parsed = await asyncio.to_thread(parse_message, raw, headers_only=headers_only)
        existing = await self.mail.find_message(mailbox.id, parsed.message_id)
        if existing is not None:
            if uid is not None and (existing.get("folder") in (None, folder)) and (
                    existing.get("uid") != uid or existing.get("uidvalidity") != uidvalidity):
                await self.mail.relocate_message(existing["id"], folder, uidvalidity, uid)
            return "duplicate"
        own = self.own_addresses(mailbox)
        outbound = (parsed.sender.address.lower() in own
                    or (mailbox.sent_folder and folder == mailbox.sent_folder))
        direction = "out" if outbound else "in"
        participants = parsed.participants()
        conversation_id, merged = await self._conversation(mailbox, parsed, participants)
        key = thread_key(mailbox.id, conversation_id)
        date = parsed.date_iso or (internaldate.astimezone(timezone.utc).isoformat()
                                   if internaldate else self.clock().isoformat())
        row_id = "mm_" + hashlib.sha256(
            f"{mailbox.id}\0{parsed.message_id}".encode()).hexdigest()[:20]
        should_wake = (wake and direction == "in" and mailbox.enabled
                       and mailbox.wake_mode in ("triage", "full"))
        if should_wake and not self._wake_allowed(mailbox):
            logger.warning("mailbox wake limit reached; stored without waking",
                           extra={"mailbox": mailbox.id, "limit": self.max_wakes_per_hour})
            should_wake = False
        attachments = [a.meta(i) for i, a in enumerate(parsed.attachments)]
        saved_dir = None
        if should_wake and self.workspace_sync is not None:
            saved_dir, attachments = await self._save_files(mailbox, key, parsed, date,
                                                            attachments)
        row = {
            "id": row_id, "account_id": mailbox.id, "conversation_id": conversation_id,
            "folder": folder, "uidvalidity": uidvalidity, "uid": uid,
            "message_id": parsed.message_id, "subject": parsed.subject,
            "from_addr": parsed.sender.address, "from_name": parsed.sender.name,
            "to_addrs": _addr_list(parsed.to), "cc_addrs": _addr_list(parsed.cc),
            "reply_to": _addr_list(parsed.reply_to), "in_reply_to": parsed.in_reply_to,
            "refs": parsed.references, "date": date, "direction": direction,
            "body_text": parsed.body_text, "body_truncated": parsed.body_truncated,
            "attachments": attachments, "size": size if size is not None else len(raw),
            "saved_dir": saved_dir, "woke": should_wake,
        }
        await self.mail.add_message(
            row, conversation={"subject": parsed.subject,
                               "norm_subject": normalize_subject(parsed.subject),
                               "participants": merged, "last_message_at": date},
            refs=[parsed.message_id, *parsed.parent_ids])
        if not should_wake:
            return "stored"
        conversation = await self.mail.get_conversation(mailbox.id, conversation_id) or {}
        payload = {
            "mailbox_id": mailbox.id, "mailbox_email": mailbox.email,
            "mailbox_label": mailbox.label, "wake_mode": mailbox.wake_mode,
            "id": row_id, "message_id": parsed.message_id,
            "conversation_id": conversation_id,
            "conversation_messages": conversation.get("message_count", 1),
            "subject": parsed.subject, "from": parsed.sender.as_dict(),
            "to": _addr_list(parsed.to), "cc": _addr_list(parsed.cc), "date": date,
            "folder": folder, "body_text": parsed.body_text[:MAX_EVENT_BODY_CHARS],
            "body_truncated": len(parsed.body_text) > MAX_EVENT_BODY_CHARS
            or parsed.body_truncated,
            "headers_only": headers_only, "attachments": attachments,
            "workspace_dir": saved_dir, "notify_user_ids": mailbox.notify_targets,
        }
        event = Event(type=EVENT_TYPE, source="mailbox", payload=payload,
                      idempotency_key=f"mail:{mailbox.id}:{parsed.message_id}",
                      thread_key=key, user_id=mailbox.owner_user_id)
        harness = "codex"
        if self.settings is not None:
            harness = await resolve_harness(self.settings.harness_source(THREAD_TYPE))
        spec = ThreadSpec(key=key, type=ThreadType(THREAD_TYPE), lane=Lane.BACKGROUND,
                          owner_user_id=mailbox.owner_user_id, harness=harness)
        await self.store.put(event, spec)
        logger.info("mail received", extra={"mailbox": mailbox.id, "thread_key": key,
                                            "attachments": len(attachments)})
        return "woke"

    def folder_for(self, mailbox: Mailbox, date: str, message_id: str, subject: str) -> str:
        """Workspace folder of one message: mail/<mailbox>/<date>-<hash>."""
        digest = hashlib.sha256(message_id.encode()).hexdigest()[:10]
        return f"mail/{mailbox.id}/{date[:10]}-{digest}"

    async def _save_files(self, mailbox: Mailbox, key: str, parsed: MailMessage, date: str,
                          attachments: list[dict]) -> tuple[str, list[dict]]:
        slug = thread_slug(key)
        folder = self.folder_for(mailbox, date, parsed.message_id, parsed.subject)
        if parsed.body_truncated or len(parsed.body_text) > MAX_EVENT_BODY_CHARS:
            await self.workspace_sync.put_file(slug, f"{folder}/body.txt",
                                               parsed.body_text.encode())
        out = []
        for meta, attachment in zip(attachments, parsed.attachments):
            path = f"{folder}/attachments/{attachment.filename}"
            await self.workspace_sync.put_file(slug, path, attachment.data)
            out.append({**meta, "path": path})
        return folder, out

    # ── outbound ───────────────────────────────────────────────────────────

    def build(self, mailbox: Mailbox, *, to: str | None, subject: str, body: str,
              cc: str | None = None, bcc: str | None = None, in_reply_to: str | None = None,
              references: str | None = None,
              files: list[tuple[str, bytes, str]] = ()) -> EmailMessage:
        message = EmailMessage()
        message["From"] = (f"{mailbox.label} <{mailbox.email}>"
                           if mailbox.label and "@" not in mailbox.label else mailbox.email)
        for name, value in (("To", to), ("Cc", cc)):
            if value:
                message[name] = value
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = make_msgid(domain=mailbox.email.rpartition("@")[2] or None)
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = references
        message.set_content(body)
        for filename, data, mime in files:
            maintype, _, subtype = (mime or "application/octet-stream").partition("/")
            message.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream",
                                   filename=filename)
        if bcc:
            message["Bcc"] = bcc  # removed before sending; kept for the record
        return message

    async def send(self, mailbox: Mailbox, message: EmailMessage,
                   recipients: list[str]) -> dict[str, Any]:
        password = await self.password(mailbox)
        record = message.as_bytes()  # with Bcc, for our own copy
        del message["Bcc"]
        data = message.as_bytes()
        client = await asyncio.to_thread(
            self.transport.open_smtp, self.params(mailbox, password, "smtp"))
        try:
            refused = await asyncio.to_thread(client.send, mailbox.email, recipients, data)
        finally:
            await asyncio.to_thread(client.close)
        result: dict[str, Any] = {"status": "sent",
                                  "message_id": str(message["Message-ID"]).strip("<>"),
                                  "refused": {k: str(v) for k, v in (refused or {}).items()}}
        if mailbox.save_sent_copy and mailbox.sent_folder:
            result["sent_copy"] = await self._append(mailbox, password, mailbox.sent_folder,
                                                     data, "\\Seen")
        await self.ingest(mailbox, record, folder=mailbox.sent_folder or "(sent)",
                          uidvalidity=None, uid=None, wake=False)
        stored = await self.mail.find_message(mailbox.id, result["message_id"])
        if stored:
            result["id"] = stored["id"]
            result["conversation_id"] = stored["conversation_id"]
        return result

    async def save_draft_copy(self, mailbox: Mailbox, message: EmailMessage) -> str:
        if not mailbox.drafts_folder:
            return "skipped: no Drafts folder found on the server"
        password = await self.password(mailbox)
        return await self._append(mailbox, password, mailbox.drafts_folder,
                                  message.as_bytes(), "\\Draft \\Seen")

    async def _append(self, mailbox: Mailbox, password: str, folder: str, data: bytes,
                      flags: str) -> str:
        try:
            session = await asyncio.to_thread(
                self.transport.open_imap, self.params(mailbox, password, "imap"))
            try:
                await asyncio.to_thread(session.append, folder, data, flags=f"({flags})")
            finally:
                await asyncio.to_thread(session.logout)
            return f"saved to {folder}"
        except MailError as exc:
            logger.warning("mailbox append failed", extra={"mailbox": mailbox.id,
                                                           "folder": folder, "error": str(exc)})
            return f"not saved to {folder}: {exc}"

    async def fetch_raw(self, mailbox: Mailbox, row: dict[str, Any]) -> bytes:
        """The message's bytes from the server (for attachments not saved at ingest)."""
        if row.get("uid") is None or not row.get("folder"):
            raise MailError("this message is not on the server (it was sent by Handler)")
        password = await self.password(mailbox)
        session = await asyncio.to_thread(
            self.transport.open_imap, self.params(mailbox, password, "imap"))
        try:
            status = await asyncio.to_thread(session.select, row["folder"], readonly=True)
            if status.uidvalidity != row.get("uidvalidity"):
                raise MailError("the folder was renumbered on the server; try again after "
                                "the next sync")
            raw = await asyncio.to_thread(session.fetch_message, int(row["uid"]))
        finally:
            await asyncio.to_thread(session.logout)
        if not raw:
            raise MailError("the message is no longer on the server")
        return raw


def new_mailbox_id() -> str:
    return new_id("mbx")


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"

