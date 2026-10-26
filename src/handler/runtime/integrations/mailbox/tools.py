"""Agent tools for IMAP/SMTP mailboxes (`mail_*`), shaped like the gmail_* tools.

Reads come from the ingested copy in the store (fast, no server round trip);
attachments not saved at ingest are fetched from the server on demand.
Drafts are Handler drafts (also appended to the server's Drafts folder when
there is one). `mail_send` is `write_external` and `explicit_allow_only`
like `gmail_send`: every send waits for a human's approval unless an exact
grant rule allows it, and the approval pins the exact recipients, subject,
body and attachment contents (by SHA-256), or a draft's exact version.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Callable

from ... import documents
from ...context import RunContext
from ...gateway.base import ToolResult, ToolSpec
from ...gateway.gmail import _email_summary, _truncate, reply_subject
from ...types import Risk, new_id
from ...uploads import sanitize_filename
from .config import Mailbox
from .message import parse_message
from .service import MailboxService, thread_key
from .store import MailStore
from .transport import MailError

MAX_RESULTS = 50
MAX_BODY_CHARS = 20_000
MAX_THREAD_CHARS = 60_000
MIN_THREAD_MESSAGE_CHARS = 4_000
MAX_TEXT_CHARS = 20_000
MAX_SEND_BYTES = 25 * 1024 * 1024
PIN_HASHES = "_approved_attachment_sha256"
PIN_DRAFT = "_approved_draft_version"
DRAFT_NOTE = ("A draft waits for a human to review and send; it is not sent, so never say "
              "it was.")
SEND_NOTE = ("Sending needs a human's approval: the call returns pending_approval and the "
             "email goes out only once someone approves it. Never say an email was sent "
             "until the result says status 'sent'.")


class ToolError(Exception):
    """A problem the agent can act on; the message is returned as the tool error."""


def _addresses(value: str | list[str] | None) -> str | None:
    if not value:
        return None
    items = [value] if isinstance(value, str) else value
    joined = ", ".join(i.strip() for i in items if i and i.strip())
    return joined or None


def _recipients(*fields: str | None) -> list[str]:
    found = [address for _, address in getaddresses([f for f in fields if f]) if "@" in address]
    return list(dict.fromkeys(found))


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in ("id", "message_id", "conversation_id", "folder", "date",
                                   "direction", "subject")}
    out["mailbox_id"] = row.get("account_id")
    out["from"] = (f"{row['from_name']} <{row['from_addr']}>" if row.get("from_name")
                   else row.get("from_addr"))
    out["to"] = [a.get("address") for a in row.get("to_addrs") or []]
    if row.get("cc_addrs"):
        out["cc"] = [a.get("address") for a in row["cc_addrs"]]
    if row.get("snippet") is not None:
        out["snippet"] = " ".join((row.get("snippet") or "").split())[:200]
    out["attachments"] = [{"index": a.get("index"), "filename": a.get("filename"),
                           "size": a.get("size")} for a in row.get("attachments") or []
                          if not a.get("inline")]
    return out


class MailTools:
    def __init__(self, service: MailboxService, mail_store: MailStore, *,
                 workspace_for: Callable[[str], Path]) -> None:
        self.service = service
        self.mail = mail_store
        self._workspace_for = workspace_for

    # ── helpers ────────────────────────────────────────────────────────────

    async def _mailbox(self, mailbox_id: str | None) -> Mailbox:
        if mailbox_id:
            mailbox = await self.mail.get_mailbox(mailbox_id)
            if mailbox is None:
                raise ToolError(f"no mailbox {mailbox_id}; see mail_list_mailboxes")
            return mailbox
        mailboxes = await self.mail.list_mailboxes()
        if len(mailboxes) == 1:
            return mailboxes[0]
        if not mailboxes:
            raise ToolError("No mailbox is connected. A site admin can add one in Crontal "
                            "under Handler settings → Mailboxes.")
        raise ToolError("Several mailboxes are connected; pass mailbox_id (see "
                        "mail_list_mailboxes).")

    async def _message(self, ref: str, mailbox_id: str | None = None) -> dict[str, Any]:
        row = await self.mail.get_message(ref) if ref.startswith("mm_") else None
        if row is None:
            ids = [mailbox_id] if mailbox_id else [m.id for m in await self.mail.list_mailboxes()]
            for candidate in ids:
                found = await self.mail.find_message(candidate, ref.strip("<>"))
                if found is not None:
                    row = await self.mail.get_message(found["id"])
                    break
        if row is None:
            raise ToolError(f"message not found: {ref}")
        return row

    async def _guard(self, ctx: RunContext, args: dict[str, Any], run) -> ToolResult:
        try:
            return await run(ctx, args)
        except (ToolError, MailError) as exc:
            return ToolResult(str(exc), is_error=True)

    def _save(self, ctx: RunContext, relative: str, data: bytes) -> str:
        workspace = self._workspace_for(ctx.thread_key)
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return str(target.relative_to(workspace))

    def _render(self, ctx: RunContext, row: dict[str, Any], limit: int) -> dict[str, Any]:
        out = _summary(row)
        out.pop("snippet", None)
        out["thread_key"] = thread_key(row["account_id"], row["conversation_id"])
        out["cc"] = [a.get("address") for a in row.get("cc_addrs") or []]
        out["reply_to"] = [a.get("address") for a in row.get("reply_to") or []]
        out["in_reply_to"] = row.get("in_reply_to") or []
        out["attachments"] = [{k: a.get(k) for k in ("index", "filename", "content_type",
                                                      "size", "inline", "path")}
                              for a in row.get("attachments") or []]
        text = row.get("body_text") or ""
        body, truncated = _truncate(text, limit)
        out["body"] = body
        if truncated:
            saved = self._save(ctx, f"mail/{row['account_id']}/{row['id']}/body.txt",
                               text.encode())
            out["body_truncated"] = (f"Body cut at {len(body)} of {len(text)} characters; "
                                     f"the full text is in {saved}.")
        if row.get("body_truncated"):
            out["note"] = "The stored body was capped at 200k characters."
        return out

    # ── read tools ─────────────────────────────────────────────────────────

    async def _list_mailboxes(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        out = []
        for mailbox in await self.mail.list_mailboxes():
            out.append({"mailbox_id": mailbox.id, "label": mailbox.label,
                        "email": mailbox.email, "folders": mailbox.folders,
                        "wake_mode": mailbox.wake_mode, "enabled": mailbox.enabled,
                        "status": mailbox.status, "last_sync_at": mailbox.last_sync_at,
                        "last_error": mailbox.last_error,
                        "messages": await self.mail.counts(mailbox.id)})
        return ToolResult({"mailboxes": out})

    async def _search(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        limit = max(1, min(int(args.get("max_results") or 20), MAX_RESULTS))
        offset = max(0, int(args.get("offset") or 0))
        if args.get("mailbox_id"):
            ids = [(await self._mailbox(args["mailbox_id"])).id]
        else:
            ids = [m.id for m in await self.mail.list_mailboxes()]
        rows = await self.mail.search(
            ids, folder=args.get("folder"), sender=args.get("from"),
            recipient=args.get("to"), subject=args.get("subject"), text=args.get("text"),
            since=args.get("after"), until=args.get("before"),
            direction=args.get("direction"), conversation_id=args.get("conversation_id"),
            limit=limit + 1, offset=offset)
        result: dict[str, Any] = {"messages": [_summary(r) for r in rows[:limit]]}
        if len(rows) > limit:
            result["next_offset"] = offset + limit
        return ToolResult(result)

    async def _read(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        row = await self._message(args["message_id"], args.get("mailbox_id"))
        return ToolResult(self._render(ctx, row, MAX_BODY_CHARS))

    async def _read_thread(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        conversation_id = args.get("conversation_id")
        mailbox_id = args.get("mailbox_id")
        if args.get("message_id"):
            row = await self._message(args["message_id"], mailbox_id)
            conversation_id, mailbox_id = row["conversation_id"], row["account_id"]
        if not conversation_id:
            raise ToolError("pass conversation_id (with mailbox_id) or message_id")
        mailbox = await self._mailbox(mailbox_id)
        summaries = await self.mail.conversation_messages(mailbox.id, conversation_id)
        limit = max(MIN_THREAD_MESSAGE_CHARS,
                    min(MAX_BODY_CHARS, MAX_THREAD_CHARS // max(len(summaries), 1)))
        messages = []
        for summary in summaries:
            full = await self.mail.get_message(summary["id"])
            if full is not None:
                messages.append(self._render(ctx, full, limit))
        return ToolResult({"mailbox_id": mailbox.id, "conversation_id": conversation_id,
                           "thread_key": thread_key(mailbox.id, conversation_id),
                           "message_count": len(messages), "messages": messages})

    async def _get_attachment(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        row = await self._message(args["message_id"], args.get("mailbox_id"))
        listed = row.get("attachments") or []
        wanted = args.get("attachment")
        match = None
        for item in listed:
            if str(item.get("index")) == str(wanted) or item.get("filename") == wanted:
                match = item
                break
        if match is None and len(listed) == 1 and wanted in (None, ""):
            match = listed[0]
        if match is None:
            raise ToolError("attachment not found; pass its index or filename from mail_read")
        workspace = self._workspace_for(ctx.thread_key)
        saved = match.get("path")
        if saved and (workspace / saved).is_file():
            data = (workspace / saved).read_bytes()
        else:
            mailbox = await self._mailbox(row["account_id"])
            raw = await self.service.fetch_raw(mailbox, row)
            parsed = await asyncio.to_thread(parse_message, raw)
            index = int(match.get("index") or 0)
            if index >= len(parsed.attachments):
                raise ToolError("the attachment is no longer in the message on the server")
            data = parsed.attachments[index].data
            name = sanitize_filename(match.get("filename"), default="attachment")
            saved = self._save(ctx, f"mail/{row['account_id']}/{row['id']}/{name}", data)
        text = await asyncio.to_thread(documents.to_text, workspace / saved,
                                       max_chars=MAX_TEXT_CHARS + 1)
        result: dict[str, Any] = {"filename": match.get("filename"),
                                  "mime_type": match.get("content_type"), "size": len(data),
                                  "saved_to": saved}
        if text:
            result["text"] = text[:MAX_TEXT_CHARS]
            if len(text) > MAX_TEXT_CHARS:
                result["text_truncated"] = True
        return ToolResult(result)

    # ── composing ──────────────────────────────────────────────────────────

    def _files(self, ctx: RunContext, paths: list[str],
               pins: dict[str, str] | None = None) -> list[tuple[str, bytes, str]]:
        workspace = self._workspace_for(ctx.thread_key).resolve()
        files = []
        for path in paths:
            source = (workspace / path).resolve()
            if workspace not in source.parents or not source.is_file():
                raise ToolError(f"attachment not found in the workspace: {path}")
            data = source.read_bytes()
            if pins is not None and pins.get(path) != hashlib.sha256(data).hexdigest():
                raise ToolError(f"attachment {path} changed after the send was approved; "
                                "not sent. Request the send again.")
            files.append((source.name, data,
                          mimetypes.guess_type(source.name)[0] or "application/octet-stream"))
        return files

    async def _resolve(self, args: dict[str, Any]) -> tuple[Mailbox, dict[str, Any]]:
        """Mailbox, recipients, subject and threading for a new email or a reply."""
        reply = None
        mailbox_id = args.get("mailbox_id")
        if args.get("reply_to_message_id"):
            reply = await self._message(args["reply_to_message_id"], mailbox_id)
            mailbox_id = reply["account_id"]
        mailbox = await self._mailbox(mailbox_id)
        to = _addresses(args.get("to"))
        subject = args.get("subject") or ""
        in_reply_to = references = conversation_id = None
        if reply is not None:
            if not to:
                sender = reply.get("reply_to") or []
                to = (", ".join(a["address"] for a in sender if a.get("address"))
                      or reply.get("from_addr"))
                if reply.get("direction") == "out":  # replying to our own message
                    to = ", ".join(a["address"] for a in reply.get("to_addrs") or [])
            subject = reply_subject(subject or reply.get("subject") or "")
            in_reply_to = f"<{reply['message_id']}>"
            refs = [f"<{r}>" for r in (reply.get("refs") or [])] + [in_reply_to]
            references = " ".join(dict.fromkeys(refs))
            conversation_id = reply["conversation_id"]
        if not to:
            raise ToolError("`to` is required unless reply_to_message_id is given")
        if not subject.strip():
            raise ToolError("`subject` is required for a new email")
        return mailbox, {"to": to, "cc": _addresses(args.get("cc")),
                         "bcc": _addresses(args.get("bcc")), "subject": subject,
                         "in_reply_to": in_reply_to, "references": references,
                         "conversation_id": conversation_id}

    def _compose(self, ctx, mailbox: Mailbox, fields: dict[str, Any], body: str,
                 attachments: list[str], pins: dict[str, str] | None = None):
        files = self._files(ctx, attachments, pins)
        if sum(len(d) for _, d, _ in files) > MAX_SEND_BYTES:
            raise ToolError("attachments exceed 25 MB; most servers refuse that")
        return self.service.build(
            mailbox, to=fields["to"], cc=fields["cc"], bcc=fields["bcc"],
            subject=fields["subject"], body=body, in_reply_to=fields["in_reply_to"],
            references=fields["references"], files=files), files

    async def _draft_reply(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        mailbox, fields = await self._resolve(args)
        attachments = list(args.get("attachments") or [])
        message, _ = self._compose(ctx, mailbox, fields, args["body"], attachments)
        draft_id = new_id("mdr")
        await self.mail.put_draft({
            "id": draft_id, "account_id": mailbox.id,
            "conversation_id": fields["conversation_id"], "thread_key": ctx.thread_key,
            "created_by": ctx.acting_as or ctx.user_id,
            "fields": {**fields, "body": args["body"], "attachments": attachments}})
        server_copy = await self.service.save_draft_copy(mailbox, message)
        return ToolResult({"draft_id": draft_id, "mailbox_id": mailbox.id,
                           "to": fields["to"], "cc": fields["cc"],
                           "subject": fields["subject"], "server_copy": server_copy,
                           "note": "Saved as a draft for a human to review. Not sent. To "
                                   "send it later, call mail_send with this draft_id."})

    async def _send_fields(self, args: dict[str, Any]) -> tuple[Mailbox, dict, str, list[str]]:
        if args.get("draft_id"):
            draft = await self.mail.get_draft(args["draft_id"])
            if draft is None or draft.get("status") != "draft":
                raise ToolError("draft not found (or already sent)")
            mailbox = await self._mailbox(draft["account_id"])
            fields = dict(draft["fields"])
            return mailbox, fields, fields.pop("body"), list(fields.pop("attachments", []))
        mailbox, fields = await self._resolve(args)
        return mailbox, fields, args.get("body") or "", list(args.get("attachments") or [])

    @staticmethod
    def _draft_version(draft: dict[str, Any] | None) -> str | None:
        if draft is None:
            return None
        return hashlib.sha256(json.dumps(draft["fields"], sort_keys=True,
                                         ensure_ascii=False).encode()).hexdigest()

    async def _preview_send(self, ctx: RunContext, args: dict[str, Any]):
        try:
            mailbox, fields, body, attachments = await self._send_fields(args)
            if not body.strip():
                raise ToolError("`body` is required")
            files = self._files(ctx, attachments)
        except (ToolError, MailError) as exc:
            return ToolResult(str(exc), is_error=True)
        pinned = {k: v for k, v in args.items() if k not in (PIN_HASHES, PIN_DRAFT)}
        if args.get("draft_id"):
            pinned[PIN_DRAFT] = self._draft_version(await self.mail.get_draft(args["draft_id"]))
        else:
            pinned.update(mailbox_id=mailbox.id, to=fields["to"], subject=fields["subject"])
        pinned[PIN_HASHES] = {path: hashlib.sha256(data).hexdigest()
                              for path, (_, data, _) in zip(attachments, files)}
        summary = _email_summary(
            f"Send email from {mailbox.email}" + (" (reply)" if fields["in_reply_to"] else ""),
            to=fields["to"], cc=fields["cc"], bcc=fields["bcc"], subject=fields["subject"],
            body=body, attachments=[(name, len(data)) for name, data, _ in files])
        return summary, pinned

    async def _send(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        if args.get("draft_id") and PIN_DRAFT in args:
            current = self._draft_version(await self.mail.get_draft(args["draft_id"]))
            if current != args[PIN_DRAFT]:
                raise ToolError("The draft changed after the send was approved; not sent. "
                                "Request the send again so the new version can be reviewed.")
        mailbox, fields, body, attachments = await self._send_fields(args)
        if not body.strip():
            raise ToolError("`body` is required")
        message, _ = self._compose(ctx, mailbox, fields, body, attachments,
                                   args.get(PIN_HASHES))
        recipients = _recipients(fields["to"], fields["cc"], fields["bcc"])
        if not recipients:
            raise ToolError("no valid recipient address")
        result = await self.service.send(mailbox, message, recipients)
        if args.get("draft_id"):
            draft = await self.mail.get_draft(args["draft_id"])
            if draft is not None:
                await self.mail.put_draft({**draft, "status": "sent"})
        return ToolResult({**result, "mailbox_id": mailbox.id, "to": fields["to"],
                           "cc": fields["cc"], "bcc": fields["bcc"],
                           "subject": fields["subject"]})

    # ── specs ──────────────────────────────────────────────────────────────

    def _spec(self, name: str, risk: Risk, description: str, schema: dict[str, Any], run,
              *, preview=None) -> ToolSpec:
        async def handler(ctx: RunContext, args: dict[str, Any]) -> ToolResult:
            return await self._guard(ctx, args, run)

        return ToolSpec(f"mail_{name}", f"mail.{name}", risk, description,
                        {"type": "object", **schema}, handler,
                        explicit_allow_only=preview is not None, approval_preview=preview)

    def tools(self) -> list[ToolSpec]:
        mailbox_id = {"type": "string", "description": "Mailbox id from mail_list_mailboxes; "
                      "optional when only one mailbox is connected."}
        addresses = {"type": ["array", "string"], "items": {"type": "string"},
                     "description": "Email addresses (a list, or one comma-separated string)."}
        compose = {
            "mailbox_id": mailbox_id, "to": addresses, "cc": addresses, "bcc": addresses,
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain-text body."},
            "reply_to_message_id": {
                "type": "string",
                "description": "Message id (mm_… or the Message-ID) to reply to: threads the "
                "email (In-Reply-To/References), uses a 'Re:' subject, and defaults `to` to "
                "the sender."},
            "attachments": {"type": "array", "items": {"type": "string"},
                            "description": "Workspace-relative paths of files to attach."},
        }
        message_ref = {"type": "string",
                       "description": "Message id (mm_…) from mail_search, or its Message-ID."}
        return [
            self._spec("list_mailboxes", Risk.READ,
                       "List the connected IMAP/SMTP mailboxes: id, address, folders, wake "
                       "mode, sync status and message counts.",
                       {"properties": {}}, self._list_mailboxes),
            self._spec(
                "search", Risk.READ,
                "Search ingested mail (newest first) by mailbox, folder, sender, recipient, "
                "subject, free text (subject, body, attachment names) and date range. "
                "Returns id, conversation_id, from, to, date, subject, snippet and "
                "attachment names. Use mail_read or mail_read_thread for bodies.",
                {"properties": {
                    "mailbox_id": mailbox_id, "folder": {"type": "string"},
                    "from": {"type": "string", "description": "Part of the sender address "
                             "or name."},
                    "to": {"type": "string", "description": "Part of a To/Cc address."},
                    "subject": {"type": "string"},
                    "text": {"type": "string"},
                    "after": {"type": "string", "description": "ISO date/time, inclusive "
                              "(e.g. 2026-10-01)."},
                    "before": {"type": "string", "description": "ISO date/time, exclusive."},
                    "direction": {"type": "string", "enum": ["in", "out"]},
                    "conversation_id": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS,
                                    "default": 20},
                    "offset": {"type": "integer", "minimum": 0}}},
                self._search),
            self._spec(
                "read", Risk.READ,
                "Read one email: headers, plain-text body (HTML converted; long bodies are "
                "cut and saved to the workspace) and its attachments (index, name, type, "
                "size, workspace path when already saved).",
                {"properties": {"message_id": message_ref, "mailbox_id": mailbox_id},
                 "required": ["message_id"]}, self._read),
            self._spec(
                "read_thread", Risk.READ,
                "Read a whole email conversation, oldest first (pass conversation_id with "
                "mailbox_id, or any message_id in it).",
                {"properties": {"conversation_id": {"type": "string"},
                                "message_id": message_ref, "mailbox_id": mailbox_id}},
                self._read_thread),
            self._spec(
                "get_attachment", Risk.READ,
                "Save an email attachment into your workspace (fetched from the server when "
                "it wasn't saved at ingest); document text (PDF, Word, Excel, CSV, ...) is "
                "extracted and returned.",
                {"properties": {"message_id": message_ref, "mailbox_id": mailbox_id,
                                "attachment": {"type": ["string", "integer"],
                                               "description": "Index or filename from "
                                               "mail_read."}},
                 "required": ["message_id"]}, self._get_attachment),
            self._spec(
                "draft_reply", Risk.WRITE_INTERNAL,
                "Prepare an email (a reply with reply_to_message_id, or a new one) as a "
                "Handler draft; it is also saved to the mailbox's Drafts folder when the "
                "server has one. " + DRAFT_NOTE + " Returns draft_id.",
                {"properties": compose, "required": ["body"]}, self._draft_reply),
            self._spec(
                "send", Risk.WRITE_EXTERNAL,
                "Send an email over SMTP from a connected mailbox: a draft (draft_id) or "
                "new fields (same as mail_draft_reply). A copy is saved to the Sent folder "
                "when the server supports it. " + SEND_NOTE + " Prefer mail_draft_reply "
                "unless the user asked to send.",
                {"properties": {**compose, "draft_id": {"type": "string"}}},
                self._send, preview=self._preview_send),
        ]
