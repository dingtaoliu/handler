"""Gmail tools — email search and reading for the agent.

Credentials setup:
1. Go to console.cloud.google.com → APIs & Services → Credentials
2. Create an OAuth 2.0 Client ID (Desktop app)
3. Download JSON → save as data/credentials/desktop.json
4. On first run, a browser opens for OAuth consent (one-time)
"""

from __future__ import annotations

import base64
import email as email_lib
import logging
import os
import re
from email.header import decode_header
from pathlib import Path

from agents import function_tool

logger = logging.getLogger("handler.gmail")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CREDENTIALS_PATH = _DATA_DIR / "credentials" / "desktop.json"
_TOKEN_PATH = _DATA_DIR / "credentials" / "token.json"


def _get_credentials():
    """Authenticate and return OAuth credentials."""
    import google.auth.exceptions
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_path = str(_CREDENTIALS_PATH)
    token_path = str(_TOKEN_PATH)

    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"Gmail credentials not found at {creds_path}. "
            "Download OAuth client JSON from Google Cloud Console → "
            "APIs & Services → Credentials, and save it there."
        )

    _CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except google.auth.exceptions.RefreshError:
                logger.warning("Token refresh failed, re-authenticating...")
                if os.path.exists(token_path):
                    os.remove(token_path)
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


def _build_service(creds):
    """Build a Gmail service with a fresh httplib2.Http (not reusable across calls)."""
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    authorized_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=120))
    return build("gmail", "v1", http=authorized_http)


# --- Email body cleaning ---


def _clean_body(body_plain: str | None, body_html: str | None) -> str:
    """Clean email body for LLM consumption. Prefers HTML → plain text conversion."""
    import html2text

    text = ""
    if body_html:
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0
        text = h.handle(body_html)
    elif body_plain:
        text = body_plain

    if not text:
        return ""

    # Remove quoted replies
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(">") or stripped.startswith("|"):
            continue
        if re.match(r"(?i)^(on .* wrote:|from:.*sent:)", stripped):
            break
        lines.append(line)
    text = "\n".join(lines)

    # Remove signatures
    text = re.sub(r"\n\s*--\s*\n.*", "", text, flags=re.DOTALL)
    text = re.sub(r"(?i)\n\s*sent from my (iphone|android|ipad|mobile).*", "", text, flags=re.DOTALL)

    # Shorten long URLs
    def _shorten(m):
        url = m.group(0)
        if len(url) > 60:
            domain = re.search(r"https?://([^/]+)", url)
            return f"[link: {domain.group(1)}]" if domain else "[link]"
        return url

    text = re.sub(r'https?://[^\s<>"{}|\\^`\[\]]+', _shorten, text)

    # Remove unsubscribe/footer boilerplate
    text = re.sub(r"(?im)\n.*unsubscribe.*$", "", text)
    text = re.sub(r"(?im)\n.*view (this email )?in (your )?browser.*$", "", text)

    # Normalize whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" +", " ", text)

    # Truncate
    if len(text) > 5000:
        text = text[:3500] + "\n\n[...truncated...]\n\n" + text[-1500:]

    return text.strip()


# --- MIME parsing ---


def _decode_mime_header(header_value: str) -> str:
    if not header_value:
        return ""
    parts = []
    for part, encoding in decode_header(header_value):
        if isinstance(part, bytes):
            parts.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(part)
    return "".join(parts)


def _extract_body(msg: email_lib.message.Message) -> tuple[str | None, str | None]:
    plain_text = None
    html = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not plain_text:
                try:
                    plain_text = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="ignore"
                    )
                except Exception:
                    pass
            elif ct == "text/html" and not html:
                try:
                    html = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="ignore"
                    )
                except Exception:
                    pass
    else:
        ct = msg.get_content_type()
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                decoded = payload.decode(
                    msg.get_content_charset() or "utf-8", errors="ignore"
                )
                if ct == "text/plain":
                    plain_text = decoded
                elif ct == "text/html":
                    html = decoded
        except Exception:
            pass

    return plain_text, html


# --- Tool factories ---


def gmail_tools() -> list:
    """Create Gmail tools. Authenticates on first call.

    Returns [search_gmail, read_email].
    Raises FileNotFoundError if credentials are not set up.
    """
    creds = _get_credentials()

    @function_tool
    def search_gmail(query: str, max_results: int = 10) -> str:
        """Search Gmail using Gmail query syntax.

        Args:
            query:       Gmail search query (same syntax as the Gmail search bar). Examples: "from:bank subject:statement", "after:2025/01/01 is:unread", "has:attachment filename:pdf", "in:inbox from:github".
            max_results: Maximum number of results to return (default 10, max 50).
        """
        try:
            svc = _build_service(creds)
            results = (
                svc.users()
                .messages()
                .list(userId="me", q=query, maxResults=min(max_results, 50))
                .execute()
            )
            messages = results.get("messages", [])
            if not messages:
                return f"No emails found for: {query}"

            # Batch fetch metadata
            batch_svc = _build_service(creds)
            output_lines = [f"Found {len(messages)} email(s) for: {query}\n"]

            for msg_stub in messages:
                msg = (
                    batch_svc.users()
                    .messages()
                    .get(
                        userId="me",
                        id=msg_stub["id"],
                        format="metadata",
                        metadataHeaders=["Subject", "From", "Date"],
                    )
                    .execute()
                )

                headers = {
                    h["name"]: h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                subject = _decode_mime_header(headers.get("Subject", "(no subject)"))
                from_header = headers.get("From", "")
                date = headers.get("Date", "")
                snippet = msg.get("snippet", "")

                output_lines.append(
                    f"---\n"
                    f"ID: {msg_stub['id']}\n"
                    f"From: {from_header}\n"
                    f"Date: {date}\n"
                    f"Subject: {subject}\n"
                    f"Preview: {snippet[:200]}"
                )

            logger.info(f"search_gmail: query={query!r} results={len(messages)}")
            return "\n".join(output_lines)

        except Exception as e:
            logger.error(f"search_gmail failed: {e}", exc_info=True)
            return f"Error searching Gmail: {e}"

    @function_tool
    def read_email(gmail_id: str) -> str:
        """Read a specific email by its Gmail ID (from search_gmail results). Returns full email with headers and cleaned body.

        Args:
            gmail_id: The Gmail message ID from search_gmail results.
        """
        try:
            msg_data = (
                _build_service(creds)
                .users()
                .messages()
                .get(userId="me", id=gmail_id, format="raw")
                .execute()
            )

            raw_bytes = base64.urlsafe_b64decode(msg_data["raw"])
            msg = email_lib.message_from_bytes(raw_bytes)

            subject = _decode_mime_header(msg.get("Subject", ""))
            from_header = msg.get("From", "")
            to_header = msg.get("To", "")
            cc_header = msg.get("Cc", "")
            date = msg.get("Date", "")

            body_plain, body_html = _extract_body(msg)
            body = _clean_body(body_plain, body_html)

            attachments = []
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_disposition() == "attachment":
                        fname = part.get_filename()
                        if fname:
                            attachments.append(fname)

            parts = [
                f"Subject: {subject}",
                f"From: {from_header}",
                f"To: {to_header}",
            ]
            if cc_header:
                parts.append(f"Cc: {cc_header}")
            parts.append(f"Date: {date}")
            if attachments:
                parts.append(f"Attachments: {', '.join(attachments)}")
            parts.append(f"\n{body}")

            output = "\n".join(parts)
            logger.info(f"read_email: id={gmail_id} subject={subject!r} ({len(output)} chars)")
            return output

        except Exception as e:
            logger.error(f"read_email failed: {e}", exc_info=True)
            return f"Error reading email {gmail_id}: {e}"

    @function_tool
    def draft_reply(gmail_id: str, body: str) -> str:
        """Draft a reply to an email. The draft is saved in Gmail Drafts for review — nothing is sent.

        Args:
            gmail_id: The Gmail message ID to reply to (from search_gmail results).
            body:     The plain-text reply body to draft.
        """
        from email.mime.text import MIMEText

        try:
            svc = _build_service(creds)

            # Fetch original message headers
            orig = (
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=gmail_id,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "To", "Message-ID"],
                )
                .execute()
            )
            headers = {
                h["name"]: h["value"]
                for h in orig.get("payload", {}).get("headers", [])
            }
            subject = headers.get("Subject", "")
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            reply_to = headers.get("From", "")
            message_id = headers.get("Message-ID", "")
            thread_id = orig.get("threadId", "")

            # Build reply MIME
            msg = MIMEText(body)
            msg["To"] = reply_to
            msg["Subject"] = subject
            if message_id:
                msg["In-Reply-To"] = message_id
                msg["References"] = message_id

            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

            draft = (
                svc.users()
                .drafts()
                .create(
                    userId="me",
                    body={
                        "message": {
                            "raw": raw,
                            "threadId": thread_id,
                        }
                    },
                )
                .execute()
            )

            logger.info(f"draft_reply: draft_id={draft['id']} replying to {gmail_id}")
            return (
                f"Draft saved (ID: {draft['id']})\n"
                f"To: {reply_to}\n"
                f"Subject: {subject}\n\n"
                f"Open Gmail Drafts to review and send."
            )

        except Exception as e:
            logger.error(f"draft_reply failed: {e}", exc_info=True)
            return f"Error creating draft: {e}"

    return [search_gmail, read_email, draft_reply]
