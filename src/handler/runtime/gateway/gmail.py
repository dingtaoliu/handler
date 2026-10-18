"""Native Gmail tools: search, read, attachments, drafts and send. Never delete.

The tools call the Gmail REST API directly with the instance's `google`
connection (gateway/google_oauth.py). There is deliberately no delete, trash,
label or modify tool. Drafts wait in the mailbox for a human to review and
send. Sending (`gmail_send`, `gmail_send_draft`) is `write_external` and
`explicit_allow_only`: every send goes through handler's approval flow unless
an exact grant rule allows it, and the approver sees the recipients, subject,
a body preview and the attachment names. What runs after approval is what was
shown: recipients and subject are resolved before the approval is parked,
attachment contents are pinned by hash, and a draft is pinned to its current
message id.

Attachments are saved into the thread's workspace under
`downloads/gmail/<message_id>/`, with document text extracted like
crontal_api does for file responses.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import re
import uuid
from collections.abc import Sequence
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import quote

import httpx

from ..context import RunContext
from ..types import Risk
from .base import ToolResult, ToolSpec
from .credentials import NeedsReauth, UpstreamAccess
from .crontal_api import _safe_name, _to_text
from .google_oauth import (
    CONNECTOR,
    DRAFT_SCOPES,
    GMAIL_MODIFY,
    INSTANCE_OWNER,
    READ_SCOPES,
    SEND_SCOPES,
    OAuthError,
)

API = "https://gmail.googleapis.com"
USER_PATH = "/gmail/v1/users/me"
MAX_RESULTS = 50
MAX_BODY_CHARS = 20_000  # one message
MAX_THREAD_CHARS = 60_000  # all bodies of a thread together
MIN_THREAD_MESSAGE_CHARS = 4_000
MAX_TEXT_CHARS = 20_000  # extracted attachment text
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024  # larger drafts go through the upload endpoint
MAX_DRAFT_BYTES = 30 * 1024 * 1024
FETCH_CONCURRENCY = 5
SUMMARY_HEADERS = ("From", "To", "Cc", "Date", "Subject")
DRAFT_NOTE = ("Drafts are saved in the mailbox for a human to review and send; a draft is "
              "not sent, so never say it was.")
SEND_NOTE = ("Sending needs a human's approval: the call returns pending_approval and the "
             "email goes out only once someone approves it. Never say an email was sent "
             "until the result says status 'sent'.")
BODY_PREVIEW_CHARS = 600
PIN_HASHES = "_approved_attachment_sha256"  # set by the approval preview, not the agent
PIN_DRAFT = "_approved_message_id"


class GmailError(Exception):
    """A Gmail API call failed; the message is safe to return to the agent."""


class GoogleCredentials(Protocol):
    async def resolve(self, connector: str, owner: str) -> UpstreamAccess | None: ...
    async def mark_needs_reauth(self, connector: str, owner: str) -> None: ...
    def invalidate(self, owner: str = INSTANCE_OWNER) -> None: ...
    async def account(self, owner: str = INSTANCE_OWNER) -> str | None: ...
    async def granted_scopes(self, owner: str = INSTANCE_OWNER) -> list[str] | None: ...


# ---- message parsing ---------------------------------------------------------

def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _headers(part: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for header in part.get("headers") or []:
        out.setdefault(header["name"].lower(), header["value"])
    return out


def _walk(part: dict[str, Any]):
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def _charset(part: dict[str, Any]) -> str:
    match = re.search(r'charset="?([\w.-]+)', _headers(part).get("content-type", ""), re.I)
    return match.group(1) if match else "utf-8"


def _part_text(part: dict[str, Any]) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    raw = _b64decode(data)
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    import html2text

    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = True
    converter.ignore_emphasis = True
    return converter.handle(html).strip()


def body_text(payload: dict[str, Any]) -> str:
    """The plain-text body; HTML parts are converted when there is no text part.

    Quoted replies and forwarded messages are kept: forwarded business mail
    is often the whole point of a message.
    """
    plain, html = [], []
    for part in _walk(payload):
        if part.get("filename") or (part.get("body") or {}).get("attachmentId"):
            continue
        mime = (part.get("mimeType") or "").lower()
        if mime == "text/plain":
            plain.append(_part_text(part))
        elif mime == "text/html":
            html.append(_part_text(part))
    if any(p.strip() for p in plain):
        return "\n\n".join(p.strip() for p in plain if p.strip())
    return "\n\n".join(html_to_text(h) for h in html if h.strip())


def attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for part in _walk(payload):
        body = part.get("body") or {}
        if part.get("filename") and (body.get("attachmentId") or body.get("data")):
            out.append({"filename": part["filename"], "mime_type": part.get("mimeType"),
                        "size": body.get("size", 0), "attachment_id": body.get("attachmentId"),
                        "part_id": part.get("partId")})
    return out


def summary(message: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload") or {}
    headers = _headers(payload)
    out: dict[str, Any] = {"id": message.get("id"), "thread_id": message.get("threadId")}
    for name in SUMMARY_HEADERS:
        if headers.get(name.lower()):
            out[name.lower()] = headers[name.lower()]
    out["snippet"] = message.get("snippet", "")
    if message.get("labelIds"):
        out["labels"] = message["labelIds"]
    out["attachments"] = [{"filename": a["filename"], "size": a["size"],
                           "mime_type": a["mime_type"]} for a in attachments(payload)]
    return out


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    cut = cut if cut > limit * 0.8 else limit
    return text[:cut].rstrip(), True


# ---- MIME building -------------------------------------------------------------

def _addresses(value: str | list[str] | None) -> str | None:
    if not value:
        return None
    items = [value] if isinstance(value, str) else value
    joined = ", ".join(i.strip() for i in items if i and i.strip())
    return joined or None


def reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if re.match(r"(?i)re\s*:", subject) else f"Re: {subject}".strip()


def build_message(
    *, to: str | None, subject: str, body: str, cc: str | None = None,
    bcc: str | None = None, sender: str | None = None, in_reply_to: str | None = None,
    references: str | None = None, files: Sequence[tuple[str, bytes, str]] = (),
) -> EmailMessage:
    message = EmailMessage(policy=policy.SMTP)
    if sender:
        message["From"] = sender
    for name, value in (("To", to), ("Cc", cc), ("Bcc", bcc)):
        if value:
            message[name] = value
    message["Subject"] = subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message.set_content(body)
    for filename, data, mime in files:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream",
                               filename=filename)
    return message


# ---- tools ------------------------------------------------------------------------

class GmailTools:
    def __init__(self, credentials: GoogleCredentials, *,
                 workspace_for: Callable[[str], Path],
                 client: httpx.AsyncClient | None = None,
                 owner_for: Callable[[RunContext], str] = lambda ctx: INSTANCE_OWNER) -> None:
        self._credentials = credentials
        self._workspace_for = workspace_for
        self._client = client or httpx.AsyncClient(timeout=60)
        self._owner_for = owner_for

    # ── HTTP ─────────────────────────────────────────────────────────────

    async def _api(self, ctx: RunContext, method: str, path: str, *,
                   params: dict[str, Any] | None = None, json_body: Any = None,
                   content: bytes | None = None, headers: dict[str, str] | None = None,
                   upload: bool = False) -> Any:
        owner = self._owner_for(ctx)
        url = f"{API}{'/upload' if upload else ''}{USER_PATH}{path}"
        for attempt in (1, 2):
            access = await self._credentials.resolve(CONNECTOR, owner)
            if access is None:
                raise GmailError("Gmail is not connected. An admin must open handler's "
                                 "Connections tab and click Connect Google.")
            response = await self._client.request(
                method, url, params=params, json=json_body, content=content,
                headers={**access.headers, **(headers or {})})
            if response.status_code == 401 and attempt == 1:
                self._credentials.invalidate(owner)
                continue
            if response.status_code == 401:
                await self._credentials.mark_needs_reauth(CONNECTOR, owner)
                raise NeedsReauth("Gmail answered 401 after a token refresh")
            if response.status_code >= 400:
                raise GmailError(_api_error(response))
            return response.json() if response.content else {}
        raise AssertionError("unreachable")

    async def _scope_problem(self, ctx: RunContext, needs: tuple[str, ...]) -> str | None:
        granted = await self._credentials.granted_scopes(self._owner_for(ctx))
        if granted is None or any(scope in granted for scope in needs):
            return None  # not connected is reported by the API call itself
        return ("The Gmail connection was granted without the permission this tool needs. "
                f"An admin must reconnect Google with the scope {GMAIL_MODIFY} (or one of: "
                + ", ".join(needs) + ") and tick every checkbox on Google's consent screen.")

    async def _guard(self, ctx: RunContext, args: dict[str, Any], run,
                     needs: tuple[str, ...]) -> ToolResult:
        try:
            if problem := await self._scope_problem(ctx, needs):
                return ToolResult(problem, is_error=True)
            return await run(ctx, args)
        except NeedsReauth:
            account = await self._credentials.account(self._owner_for(ctx))
            return ToolResult(
                "The Gmail connection" + (f" ({account})" if account else "")
                + " needs to be reconnected: Google no longer accepts its sign-in. "
                "Tell the user that an admin must open handler's Connections tab and "
                "click Reconnect Google. Do not retry until then.", is_error=True)
        except (GmailError, OAuthError) as exc:
            return ToolResult(str(exc), is_error=True)
        except httpx.HTTPError as exc:
            return ToolResult(f"Gmail request failed: {exc}", is_error=True)

    async def _get_messages(self, ctx: RunContext, ids: list[str]) -> list[dict[str, Any]]:
        limit = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(message_id: str) -> dict[str, Any]:
            async with limit:
                return await self._api(ctx, "GET", f"/messages/{quote(message_id)}",
                                       params={"format": "full"})

        return list(await asyncio.gather(*(one(i) for i in ids)))

    # ── read tools ───────────────────────────────────────────────────────

    async def _search(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        max_results = max(1, min(int(args.get("max_results") or 20), MAX_RESULTS))
        params: dict[str, Any] = {"q": args["query"], "maxResults": max_results}
        if args.get("page_token"):
            params["pageToken"] = args["page_token"]
        listed = await self._api(ctx, "GET", "/messages", params=params)
        ids = [m["id"] for m in listed.get("messages") or []]
        messages = await self._get_messages(ctx, ids)
        result: dict[str, Any] = {"messages": [summary(m) for m in messages],
                                  "result_size_estimate": listed.get("resultSizeEstimate", 0)}
        if listed.get("nextPageToken"):
            result["next_page_token"] = listed["nextPageToken"]
        return ToolResult(result)

    def _render(self, ctx: RunContext, message: dict[str, Any], limit: int) -> dict[str, Any]:
        payload = message.get("payload") or {}
        headers = _headers(payload)
        out = summary(message)
        for name in ("reply-to", "message-id", "in-reply-to"):
            if headers.get(name):
                out[name.replace("-", "_")] = headers[name]
        out["attachments"] = attachments(payload)
        text = body_text(payload)
        body, truncated = _truncate(text, limit)
        out["body"] = body
        if truncated:
            saved = self._save(ctx, message["id"], "body.txt", text.encode())
            out["body_truncated"] = (f"Body cut at {len(body)} of {len(text)} characters; "
                                     f"the full text is in {saved}.")
        return out

    async def _read_message(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        message, = await self._get_messages(ctx, [args["message_id"]])
        return ToolResult(self._render(ctx, message, MAX_BODY_CHARS))

    async def _read_thread(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        thread = await self._api(ctx, "GET", f"/threads/{quote(args['thread_id'])}",
                                 params={"format": "full"})
        messages = thread.get("messages") or []
        limit = max(MIN_THREAD_MESSAGE_CHARS,
                    min(MAX_BODY_CHARS, MAX_THREAD_CHARS // max(len(messages), 1)))
        return ToolResult({"thread_id": thread.get("id"), "message_count": len(messages),
                           "messages": [self._render(ctx, m, limit) for m in messages]})

    async def _get_attachment(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        message_id, wanted = args["message_id"], args["attachment_id"]
        message, = await self._get_messages(ctx, [message_id])
        listed = attachments(message.get("payload") or {})
        # Gmail may hand out a different attachmentId on each fetch; also accept a part id.
        match = next((a for a in listed if wanted in (a["attachment_id"], a["part_id"])), None)
        if match is None and len(listed) == 1:
            match = listed[0]
        filename = (args.get("filename") or (match or {}).get("filename")
                    or f"attachment-{wanted[:12]}")
        mime = (match or {}).get("mime_type") or mimetypes.guess_type(filename)[0]
        if match and not match["attachment_id"]:
            data = _b64decode(next(
                p["body"]["data"] for p in _walk(message["payload"])
                if p.get("partId") == match["part_id"]))
        else:
            attachment_id = match["attachment_id"] if match else wanted
            body = await self._api(
                ctx, "GET", f"/messages/{quote(message_id)}/attachments/{quote(attachment_id)}")
            data = _b64decode(body.get("data", ""))
        saved = self._save(ctx, message_id, filename, data)
        workspace = self._workspace_for(ctx.thread_key)
        text = await asyncio.to_thread(_to_text, workspace / saved)
        result: dict[str, Any] = {"filename": filename, "mime_type": mime, "size": len(data),
                                  "saved_to": saved}
        if text:
            result["text"] = text[:MAX_TEXT_CHARS]
            if len(text) > MAX_TEXT_CHARS:
                result["text_truncated"] = True
        return ToolResult(result)

    def _save(self, ctx: RunContext, message_id: str, name: str, data: bytes) -> str:
        workspace = self._workspace_for(ctx.thread_key)
        target_dir = workspace / "downloads" / "gmail" / _safe_name(message_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / _safe_name(name)
        target.write_bytes(data)
        return str(target.relative_to(workspace))

    # ── drafts ─────────────────────────────────────────────────────────────

    def _files(self, ctx: RunContext, paths: list[str],
               pins: dict[str, str] | None = None) -> list[tuple[str, bytes, str]]:
        """Workspace files as (name, bytes, mime). With `pins` (path → sha256 from
        the approval preview), a file whose content changed since is refused."""
        workspace = self._workspace_for(ctx.thread_key).resolve()
        files = []
        for path in paths:
            source = (workspace / path).resolve()
            if workspace not in source.parents or not source.is_file():
                raise GmailError(f"attachment not found in the workspace: {path}")
            data = source.read_bytes()
            if pins is not None and pins.get(path) != hashlib.sha256(data).hexdigest():
                raise GmailError(f"attachment {path} changed after the send was approved; "
                                 "not sent. Request the send again.")
            mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            files.append((source.name, data, mime))
        return files

    async def _reply_headers(self, ctx: RunContext, message_id: str) -> dict[str, Any]:
        original = await self._api(ctx, "GET", f"/messages/{quote(message_id)}", params={
            "format": "metadata",
            "metadataHeaders": ["Message-ID", "References", "Subject", "From", "Reply-To"]})
        headers = _headers(original.get("payload") or {})
        msgid = headers.get("message-id")
        references = " ".join(x for x in (headers.get("references"), msgid) if x) or None
        return {"thread_id": original.get("threadId"), "in_reply_to": msgid,
                "references": references, "subject": headers.get("subject", ""),
                "to": headers.get("reply-to") or headers.get("from")}

    async def _upload_draft(self, ctx: RunContext, method: str, path: str,
                            message: EmailMessage, thread_id: str | None,
                            draft_id: str | None = None) -> dict[str, Any]:
        meta: dict[str, Any] = {"message": {"threadId": thread_id} if thread_id else {}}
        if draft_id:
            meta["id"] = draft_id
        return await self._upload(ctx, method, path, message, meta, meta["message"])

    async def _upload(self, ctx: RunContext, method: str, path: str, message: EmailMessage,
                      meta: dict[str, Any], raw_into: dict[str, Any]) -> dict[str, Any]:
        """Send `meta` with the MIME message: as `raw_into["raw"]` when small, else
        through Gmail's multipart upload endpoint."""
        raw = message.as_bytes()
        if len(raw) > MAX_DRAFT_BYTES:
            raise GmailError(f"The email is {len(raw) // (1024 * 1024)} MB; Gmail allows "
                             "about 25 MB of attachments.")
        if len(raw) <= SIMPLE_UPLOAD_LIMIT:
            raw_into["raw"] = base64.urlsafe_b64encode(raw).decode()
            return await self._api(ctx, method, path, json_body=meta)
        boundary = f"handler-{uuid.uuid4().hex}"
        content = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                   f"{json.dumps(meta)}\r\n--{boundary}\r\n"
                   "Content-Type: message/rfc822\r\n\r\n").encode() + raw + (
                   f"\r\n--{boundary}--").encode()
        return await self._api(
            ctx, method, path, params={"uploadType": "multipart"}, content=content,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"}, upload=True)

    async def _draft_result(self, ctx: RunContext, draft: dict[str, Any]) -> dict[str, Any]:
        message = draft.get("message") or {}
        account = await self._credentials.account(self._owner_for(ctx))
        link = "https://mail.google.com/mail/" + (
            f"?authuser={quote(account)}" if account else "") + (
            f"#drafts?compose={message.get('id')}")
        return {"draft_id": draft.get("id"), "message_id": message.get("id"),
                "thread_id": message.get("threadId"), "gmail_link": link,
                "note": "Saved as a draft for a human to review and send. Not sent."}

    async def _resolve_new(self, ctx: RunContext, args: dict[str, Any]) -> dict[str, Any]:
        """Recipients, subject and threading for a new email or a reply."""
        reply = (await self._reply_headers(ctx, args["reply_to_message_id"])
                 if args.get("reply_to_message_id") else {})
        to = _addresses(args.get("to")) or reply.get("to")
        if not to:
            raise GmailError("`to` is required unless reply_to_message_id is given")
        subject = args.get("subject") or ""
        if reply:
            subject = reply_subject(subject or reply.get("subject", ""))
        elif not subject.strip():
            raise GmailError("`subject` is required for a new email")
        return {"to": to, "subject": subject, "cc": _addresses(args.get("cc")),
                "bcc": _addresses(args.get("bcc")), "thread_id": reply.get("thread_id"),
                "in_reply_to": reply.get("in_reply_to"),
                "references": reply.get("references")}

    async def _compose(self, ctx: RunContext, args: dict[str, Any],
                       pins: dict[str, str] | None = None) -> tuple[EmailMessage, str | None]:
        fields = await self._resolve_new(ctx, args)
        message = build_message(
            to=fields["to"], subject=fields["subject"], body=args["body"], cc=fields["cc"],
            bcc=fields["bcc"], in_reply_to=fields["in_reply_to"],
            references=fields["references"],
            files=self._files(ctx, args.get("attachments") or [], pins))
        return message, fields["thread_id"]

    async def _create_draft(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        message, thread_id = await self._compose(ctx, args)
        draft = await self._upload_draft(ctx, "POST", "/drafts", message, thread_id)
        return ToolResult(await self._draft_result(ctx, draft))

    async def _update_draft(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        draft_id = args["draft_id"]
        current = await self._api(ctx, "GET", f"/drafts/{quote(draft_id)}",
                                  params={"format": "raw"})
        old_message = current.get("message") or {}
        old = BytesParser(policy=policy.default).parsebytes(
            _b64decode(old_message.get("raw", "")))
        thread_id = old_message.get("threadId")
        in_reply_to, references = old.get("In-Reply-To"), old.get("References")
        subject = args.get("subject") or old.get("Subject") or ""
        if args.get("reply_to_message_id"):
            reply = await self._reply_headers(ctx, args["reply_to_message_id"])
            thread_id, in_reply_to = reply["thread_id"], reply["in_reply_to"]
            references = reply["references"]
            subject = reply_subject(args.get("subject") or reply["subject"])
        if "body" in args and args["body"] is not None:
            body = args["body"]
        else:
            part = old.get_body(("plain", "html"))
            body = "" if part is None else part.get_content()
            if part is not None and part.get_content_subtype() == "html":
                body = html_to_text(body)
            body = body.replace("\r\n", "\n")
        if args.get("attachments") is not None:
            files = self._files(ctx, args["attachments"])
        else:
            files = [(a.get_filename() or "attachment", a.get_content(),
                      a.get_content_type()) for a in old.iter_attachments()]
            files = [(n, d if isinstance(d, bytes) else str(d).encode(), m)
                     for n, d, m in files]

        def pick(key: str, header: str) -> str | None:
            return _addresses(args[key]) if args.get(key) is not None else old.get(header)

        message = build_message(
            to=pick("to", "To"), subject=subject, body=body, cc=pick("cc", "Cc"),
            bcc=pick("bcc", "Bcc"), sender=old.get("From"), in_reply_to=in_reply_to,
            references=references, files=files)
        draft = await self._upload_draft(ctx, "PUT", f"/drafts/{quote(draft_id)}", message,
                                         thread_id, draft_id=draft_id)
        return ToolResult(await self._draft_result(ctx, draft))

    async def _list_drafts(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        max_results = max(1, min(int(args.get("max_results") or 20), MAX_RESULTS))
        listed = await self._api(ctx, "GET", "/drafts", params={"maxResults": max_results})
        limit = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(draft_id: str) -> dict[str, Any]:
            async with limit:
                draft = await self._api(ctx, "GET", f"/drafts/{quote(draft_id)}",
                                        params={"format": "metadata"})
            out = await self._draft_result(ctx, draft)
            out.pop("note")
            info = summary(draft.get("message") or {})
            out.update({k: info[k] for k in ("to", "cc", "subject", "date", "snippet")
                        if k in info})
            return out

        drafts = await asyncio.gather(*(one(d["id"]) for d in listed.get("drafts") or []))
        return ToolResult({"drafts": list(drafts)})

    # ── send (approval-gated) ────────────────────────────────────────────

    async def _sent_result(self, ctx: RunContext, sent: dict[str, Any],
                           shown: dict[str, Any]) -> dict[str, Any]:
        account = await self._credentials.account(self._owner_for(ctx))
        link = "https://mail.google.com/mail/" + (
            f"?authuser={quote(account)}" if account else "") + f"#all/{sent.get('id')}"
        return {"status": "sent", "message_id": sent.get("id"),
                "thread_id": sent.get("threadId"), "gmail_link": link,
                **{k: v for k, v in shown.items() if v}}

    async def _send(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        pins = args.get(PIN_HASHES)
        message, thread_id = await self._compose(ctx, args, pins)
        meta: dict[str, Any] = {"threadId": thread_id} if thread_id else {}
        sent = await self._upload(ctx, "POST", "/messages/send", message, meta, meta)
        return ToolResult(await self._sent_result(ctx, sent, {
            "to": message["To"], "cc": message["Cc"], "bcc": message["Bcc"],
            "subject": message["Subject"]}))

    async def _preview_send(self, ctx: RunContext,
                            args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        fields = await self._resolve_new(ctx, args)
        paths = args.get("attachments") or []
        files = self._files(ctx, paths)
        pinned = {**args, "to": fields["to"], "subject": fields["subject"],
                  PIN_HASHES: {path: hashlib.sha256(data).hexdigest()
                               for path, (_, data, _) in zip(paths, files)}}
        return _email_summary(
            "Send email" + (" (reply)" if fields["in_reply_to"] else ""),
            to=fields["to"], cc=fields["cc"], bcc=fields["bcc"],
            subject=fields["subject"], body=args["body"],
            attachments=[(name, len(data)) for name, data, _ in files]), pinned

    async def _draft_message(self, ctx: RunContext, draft_id: str):
        current = await self._api(ctx, "GET", f"/drafts/{quote(draft_id)}",
                                  params={"format": "raw"})
        message = current.get("message") or {}
        parsed = BytesParser(policy=policy.default).parsebytes(
            _b64decode(message.get("raw", "")))
        return message, parsed

    async def _send_draft(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        draft_id = args["draft_id"]
        message, parsed = await self._draft_message(ctx, draft_id)
        pinned = args.get(PIN_DRAFT)
        if pinned is not None and message.get("id") != pinned:
            raise GmailError("The draft changed after the send was approved; not sent. "
                             "Request the send again so the new version can be reviewed.")
        sent = await self._api(ctx, "POST", "/drafts/send", json_body={"id": draft_id})
        return ToolResult(await self._sent_result(ctx, sent, {
            "draft_id": draft_id, "to": parsed.get("To"), "cc": parsed.get("Cc"),
            "bcc": parsed.get("Bcc"), "subject": parsed.get("Subject")}))

    async def _preview_send_draft(self, ctx: RunContext,
                                  args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        message, parsed = await self._draft_message(ctx, args["draft_id"])
        if not parsed.get("To") and not parsed.get("Cc") and not parsed.get("Bcc"):
            raise GmailError("The draft has no recipients; set `to` with gmail_update_draft.")
        part = parsed.get_body(("plain", "html"))
        body = "" if part is None else part.get_content()
        if part is not None and part.get_content_subtype() == "html":
            body = html_to_text(body)
        files = [(a.get_filename() or "attachment", len(a.get_payload(decode=True) or b""))
                 for a in parsed.iter_attachments()]
        summary = _email_summary(
            f"Send draft {args['draft_id']}", to=parsed.get("To"), cc=parsed.get("Cc"),
            bcc=parsed.get("Bcc"), subject=parsed.get("Subject"), body=body,
            attachments=files)
        return summary, {**args, PIN_DRAFT: message.get("id")}

    def _preview(self, run, needs: tuple[str, ...]):
        """An ApprovalPreview: errors become a ToolResult, so nothing is parked."""

        async def preview(ctx: RunContext, args: dict[str, Any]):
            result: dict[str, Any] = {}

            async def capture(ctx: RunContext, args: dict[str, Any]) -> ToolResult:
                result["value"] = await run(ctx, args)
                return ToolResult("ok")

            outcome = await self._guard(ctx, args, capture, needs)
            return result.get("value", outcome)

        return preview

    # ── specs ─────────────────────────────────────────────────────────────

    def _spec(self, name: str, risk: Risk, description: str, schema: dict[str, Any],
              run, *, needs: tuple[str, ...] | None = None, preview=None) -> ToolSpec:
        needs = needs or (DRAFT_SCOPES if "draft" in name else READ_SCOPES)

        async def handler(ctx: RunContext, args: dict[str, Any]) -> ToolResult:
            return await self._guard(ctx, args, run, needs)

        return ToolSpec(
            f"gmail_{name}", f"gmail.{name}", risk, description,
            {"type": "object", **schema}, handler,
            explicit_allow_only=preview is not None,
            approval_preview=self._preview(preview, needs) if preview else None)

    def tools(self) -> list[ToolSpec]:
        addresses = {"type": ["array", "string"], "items": {"type": "string"},
                     "description": "Email addresses (a list, or one comma-separated string)."}
        draft_fields = {
            "to": addresses, "cc": addresses, "bcc": addresses,
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain-text body."},
            "reply_to_message_id": {
                "type": "string",
                "description": "Gmail message id to reply to: threads the draft, sets "
                "In-Reply-To/References and a 'Re:' subject, and defaults `to` to the "
                "sender."},
            "attachments": {"type": "array", "items": {"type": "string"},
                            "description": "Workspace-relative paths of files to attach."},
        }
        max_results = {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS}
        return [
            self._spec(
                "search", Risk.READ,
                "Search the shared Gmail mailbox with Gmail search syntax (from:, to:, "
                "subject:, has:attachment, filename:pdf, after:2026/01/01, label:, quotes "
                "for phrases). Returns id, thread_id, from, to, date, subject, snippet and "
                "attachment names/sizes. Use gmail_read_thread or gmail_read_message for "
                "bodies.",
                {"properties": {
                    "query": {"type": "string", "description": "Gmail search query."},
                    "max_results": {**max_results, "default": 20},
                    "page_token": {"type": "string",
                                   "description": "next_page_token from a previous search."}},
                 "required": ["query"]},
                self._search),
            self._spec(
                "read_thread", Risk.READ,
                "Read a whole Gmail thread: headers, plain-text body of each message (HTML "
                "converted, quoted and forwarded text kept; long bodies are cut and saved to "
                "the workspace) and attachments with their attachment_id.",
                {"properties": {"thread_id": {"type": "string"}}, "required": ["thread_id"]},
                self._read_thread),
            self._spec(
                "read_message", Risk.READ,
                "Read one Gmail message: headers, plain-text body (HTML converted, quoted and "
                "forwarded text kept; long bodies are cut and saved to the workspace) and "
                "attachments with their attachment_id.",
                {"properties": {"message_id": {"type": "string"}},
                 "required": ["message_id"]},
                self._read_message),
            self._spec(
                "get_attachment", Risk.READ,
                "Download a Gmail attachment into your workspace under downloads/gmail/; "
                "document text (PDF, Word, Excel, CSV, ...) is extracted and returned.",
                {"properties": {
                    "message_id": {"type": "string"},
                    "attachment_id": {"type": "string",
                                      "description": "attachment_id (or part_id) from "
                                      "gmail_read_message/gmail_read_thread."},
                    "filename": {"type": "string",
                                 "description": "Optional file name to save as."}},
                 "required": ["message_id", "attachment_id"]},
                self._get_attachment),
            self._spec(
                "create_draft", Risk.WRITE_INTERNAL,
                "Create an email draft in the shared Gmail mailbox (new email, or a reply "
                "with reply_to_message_id). " + DRAFT_NOTE + " Returns the draft id and a "
                "Gmail link to it.",
                {"properties": draft_fields, "required": ["body"]},
                self._create_draft),
            self._spec(
                "update_draft", Risk.WRITE_INTERNAL,
                "Change an existing Gmail draft. Only the fields you pass change; "
                "`attachments` replaces the draft's attachments. " + DRAFT_NOTE,
                {"properties": {"draft_id": {"type": "string"}, **draft_fields},
                 "required": ["draft_id"]},
                self._update_draft),
            self._spec(
                "list_drafts", Risk.READ,
                "List drafts in the shared Gmail mailbox (id, to, subject, snippet, link). "
                + DRAFT_NOTE,
                {"properties": {"max_results": {**max_results, "default": 20}}},
                self._list_drafts),
            self._spec(
                "send", Risk.WRITE_EXTERNAL,
                "Send an email from the shared Gmail mailbox: a new email, or a reply with "
                "reply_to_message_id (threaded, In-Reply-To/References set, 'Re:' subject, "
                "`to` defaults to the sender). Attachments are workspace paths. "
                + SEND_NOTE + " Prefer gmail_create_draft unless the user asked to send.",
                {"properties": draft_fields, "required": ["body"]},
                self._send, needs=SEND_SCOPES, preview=self._preview_send),
            self._spec(
                "send_draft", Risk.WRITE_EXTERNAL,
                "Send an existing Gmail draft as it is now (see gmail_list_drafts). "
                + SEND_NOTE + " If the draft is changed after approval, the send is "
                "refused.",
                {"properties": {"draft_id": {"type": "string"}}, "required": ["draft_id"]},
                self._send_draft, needs=SEND_SCOPES, preview=self._preview_send_draft),
        ]


def _email_summary(title: str, *, to: str | None, cc: str | None, bcc: str | None,
                   subject: str | None, body: str,
                   attachments: list[tuple[str, int]]) -> str:
    """What an approver sees for a send: recipients, subject, body preview, files."""
    lines = [title, f"To: {to or '-'}"]
    if cc:
        lines.append(f"Cc: {cc}")
    if bcc:
        lines.append(f"Bcc: {bcc}")
    lines.append(f"Subject: {subject or '(no subject)'}")
    if attachments:
        lines.append("Attachments: " + ", ".join(
            f"{name} ({size:,} bytes)" for name, size in attachments))
    else:
        lines.append("Attachments: none")
    text = (body or "").strip()
    preview, cut = _truncate(text, BODY_PREVIEW_CHARS)
    lines.append("Body:\n" + preview + (f"\n… ({len(text):,} characters in all)" if cut else ""))
    return "\n".join(lines)


def _api_error(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
    except (ValueError, AttributeError):
        message = response.text[:300]
    status = response.status_code
    if status == 403 and "insufficient" in (message or "").lower():
        return ("Gmail refused: the connection lacks a required permission. An admin must "
                "reconnect Google and grant every permission.")
    if status == 404:
        return f"Gmail: not found ({message})"
    return f"Gmail API error {status}: {message}"
