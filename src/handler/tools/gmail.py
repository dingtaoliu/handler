"""Gmail tool — single entry point for email search, reading, and drafting.

Credentials setup:
1. Go to console.cloud.google.com → APIs & Services → Credentials
2. Create an OAuth 2.0 Client ID (Desktop app)
3. Download JSON → save as data/credentials/desktop.json
4. On first run, a browser opens for OAuth consent (one-time)
"""

from __future__ import annotations

import base64
import email as email_lib
import json
import logging
import os
import re
from email.header import decode_header
from email.message import Message as EmailMessage
from pathlib import Path

import sys

from agents import function_tool

from ..google_oauth import build_console_authorization_url
from ..paths import DATA_DIR as _DATA_DIR, GMAIL_UPLOAD_DIR
from ..users import get_default_user, get_user

logger = logging.getLogger("handler.tools.gmail")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]
_CREDENTIALS_PATH = _DATA_DIR / "credentials" / "desktop.json"
_TOKEN_PATH = _DATA_DIR / "credentials" / "token.json"


class OAuthRequired(Exception):
    def __init__(self, url: str):
        self.url = url
        super().__init__(url)


def _is_headless() -> bool:
    if os.environ.get("HANDLER_AUTH_CONSOLE"):
        return True
    if not sys.stdout.isatty():
        return True
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return True
    return False


def _user_credentials_dir(user_id: str | None) -> Path:
    if user_id:
        return get_user(user_id).credentials_dir
    return get_default_user().credentials_dir


def _token_path(user_id: str | None = None, conversation_id: str | None = None) -> str:
    if user_id:
        return str(_user_credentials_dir(user_id) / "gmail_token.json")
    if conversation_id:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", conversation_id)
        legacy = _CREDENTIALS_PATH.parent / f"gmail_token_{safe}.json"
        if legacy.exists():
            return str(legacy)
    return str(_TOKEN_PATH)


def _auth_user_context(run_ctx=None) -> tuple[str, str]:
    user_id = run_ctx.user_id if run_ctx else None
    user = get_user(user_id)
    return user.id, user.display_name


def _missing_credentials_message(run_ctx=None) -> str:
    user_id, display_name = _auth_user_context(run_ctx)
    return (
        f"Gmail is available but Google OAuth is not configured yet for {display_name}.\n\n"
        f"1. Save your Google OAuth desktop client JSON to: {_CREDENTIALS_PATH}\n"
        f"   You can also run `handler init` and provide desktop.json there.\n"
        f"2. Authorize Gmail for this user on the server:\n\n"
        f"  handler auth gmail --console --user {user_id}\n\n"
        "If the server has a browser session, you can omit --console."
    )


def _oauth_required_message(auth_url: str, run_ctx=None) -> str:
    user_id, display_name = _auth_user_context(run_ctx)
    return (
        f"Gmail authentication is required for {display_name}.\n\n"
        f"Run this on the server:\n\n"
        f"  handler auth gmail --console --user {user_id}\n\n"
        f"If you have a browser on the server, you can omit --console.\n"
        f"Or open this URL to complete the OAuth flow:\n{auth_url}"
    )


_HELP_TEXT = """\
gmail — search, read, draft replies, and manage labels & filters.

Actions:
  search        — Search Gmail using Gmail query syntax.
                  Params: query (required), max_results (default 10, up to 500), page_token (optional).
                  If results include a page_token, pass it to fetch the next page.
                  Query examples: "from:bank subject:statement", "after:2025/01/01 is:unread",
                  "has:attachment filename:pdf", "in:inbox from:github".
    read          — Read a specific email by its Gmail ID (from search results).
                                    Params: gmail_id (required), download_attachments (optional, default false).
                                    If download_attachments is true, attachments are saved to data/uploads/gmail/ and local paths are returned.
  draft_reply   — Draft a reply to an email. Saved in Gmail Drafts — nothing is sent.
                  Params: gmail_id (required), body (required), cc (optional, comma-separated),
                  reply_all (optional bool, default false — if true, replies to all original recipients),
                  draft_id (optional — if provided, updates that existing draft instead of creating a new one).
  list_drafts   — List existing Gmail drafts with their IDs and subjects.
                  Params: max_results (default 10).
  list_labels   — List all Gmail labels (system and user-created).
  create_label  — Create a new label.
                  Params: label_name (required).
  update_label  — Rename a label.
                  Params: label_id (required), label_name (new name, required).
  delete_label  — Delete a label.
                  Params: label_id (required).
  list_filters  — List all Gmail filters.
  create_filter — Create a new filter.
                  Params: filter_criteria (required JSON), filter_actions (required JSON).
                  Criteria fields: from, to, subject, query, negatedQuery, hasAttachment (bool).
                  Action fields: addLabelIds (list), removeLabelIds (list), forward (email),
                  markRead (bool → maps to removeLabelIds: ["UNREAD"]),
                  archive (bool → maps to removeLabelIds: ["INBOX"]),
                  star (bool → maps to addLabelIds: ["STARRED"]).
                  Example: filter_criteria='{"from": "noreply@github.com"}',
                  filter_actions='{"addLabelIds": ["Label_123"], "archive": true}'.
  delete_filter — Delete a filter.
                  Params: filter_id (required)."""


def _get_credentials(
    user_id: str | None = None,
    conversation_id: str | None = None,
):
    """Authenticate and return OAuth credentials.

    Raises OAuthRequired (with auth URL) when running headless and no token exists.
    """
    import google.auth.exceptions
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_path = str(_CREDENTIALS_PATH)
    token_path = _token_path(user_id=user_id, conversation_id=conversation_id)

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
                logger.warning("Gmail token refresh failed, re-authenticating...")
                os.remove(token_path)
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            if _is_headless():
                auth_url = build_console_authorization_url(flow)
                raise OAuthRequired(auth_url)
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
    """Clean email body for LLM consumption. Prefers plain text; strips HTML as fallback."""
    from bs4 import BeautifulSoup

    text = ""
    if body_plain:
        text = body_plain
    elif body_html:
        soup = BeautifulSoup(body_html, "html.parser")
        for tag in soup(["script", "style", "head", "blockquote"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)

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
    text = re.sub(
        r"(?i)\n\s*sent from my (iphone|android|ipad|mobile).*",
        "",
        text,
        flags=re.DOTALL,
    )

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


def _extract_body(msg: EmailMessage) -> tuple[str | None, str | None]:
    plain_text = None
    html = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not plain_text:
                try:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        plain_text = payload.decode(
                            part.get_content_charset() or "utf-8", errors="ignore"
                        )
                except Exception:
                    pass
            elif ct == "text/html" and not html:
                try:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        html = payload.decode(
                            part.get_content_charset() or "utf-8", errors="ignore"
                        )
                except Exception:
                    pass
    else:
        ct = msg.get_content_type()
        try:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
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


def _safe_attachment_name(name: str | None, index: int) -> str:
    if not name:
        return f"attachment-{index}"

    decoded = _decode_mime_header(name).strip()
    safe = Path(decoded).name
    if not safe or safe in {".", ".."}:
        return f"attachment-{index}"
    return safe


def _save_attachments(msg: EmailMessage, gmail_id: str) -> list[tuple[str, str]]:
    GMAIL_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[tuple[str, str]] = []

    if not msg.is_multipart():
        return saved

    attachment_index = 0
    for part in msg.walk():
        if part.get_content_disposition() != "attachment":
            continue

        attachment_index += 1
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue

        filename = _safe_attachment_name(part.get_filename(), attachment_index)
        dest = GMAIL_UPLOAD_DIR / f"gmail_{gmail_id}_{attachment_index}_{filename}"
        dest.write_bytes(payload)
        saved.append((filename, str(dest.resolve())))

    return saved


# --- Tool factory ---


def gmail_tool(run_ctx=None):
    """Create a single gmail tool. Authenticates per-user on first call."""

    def _creds():
        conversation_id = run_ctx.conversation_id if run_ctx else None
        user_id = run_ctx.user_id if run_ctx else None
        return _get_credentials(user_id=user_id, conversation_id=conversation_id)

    def _action_search(query: str, max_results: int, page_token: str) -> str:
        creds = _creds()
        svc = _build_service(_creds())
        list_kwargs: dict = {
            "userId": "me",
            "q": query,
            "maxResults": min(max_results, 500),
        }
        if page_token:
            list_kwargs["pageToken"] = page_token

        results = svc.users().messages().list(**list_kwargs).execute()
        messages = results.get("messages", [])
        next_page_token = results.get("nextPageToken")

        if not messages:
            return f"No emails found for: {query}"

        batch_svc = _build_service(creds)

        # Pagination status header — placed first so it's never missed
        if next_page_token:
            header = (
                f"PARTIAL RESULTS: showing {len(messages)} emails (more exist).\n"
                f"NEXT ACTION: call search again with page_token={next_page_token!r} to get the next page.\n"
                f"Do NOT repeat this query without page_token or you will get the same results.\n"
            )
        else:
            header = (
                f"ALL RESULTS: {len(messages)} email(s) for: {query} (no more pages)\n"
            )

        output_lines = [header]

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
                h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])
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

        logger.info(
            f"gmail search: query={query!r} results={len(messages)} "
            f"has_more={next_page_token is not None}"
        )
        return "\n".join(output_lines)

    def _action_read(gmail_id: str, download_attachments: bool) -> str:
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
        downloaded_attachments: list[tuple[str, str]] = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_disposition() == "attachment":
                    fname = part.get_filename()
                    if fname:
                        attachments.append(_decode_mime_header(fname))

        if download_attachments:
            downloaded_attachments = _save_attachments(msg, gmail_id)

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
        if download_attachments:
            if downloaded_attachments:
                parts.append("Downloaded attachments:")
                for filename, saved_path in downloaded_attachments:
                    parts.append(f"- {filename}: {saved_path}")
            else:
                parts.append("Downloaded attachments: none")
        parts.append(f"\n{body}")

        output = "\n".join(parts)
        logger.info(
            f"gmail read: id={gmail_id} subject={subject!r} download_attachments={download_attachments} ({len(output)} chars)"
        )
        return output

    def _action_list_drafts(max_results: int) -> str:
        svc = _build_service(_creds())
        results = (
            svc.users()
            .drafts()
            .list(userId="me", maxResults=min(max_results, 100))
            .execute()
        )
        drafts = results.get("drafts", [])
        if not drafts:
            return "No drafts found."

        lines = [f"Found {len(drafts)} draft(s):\n"]
        for d in drafts:
            detail = (
                svc.users()
                .drafts()
                .get(userId="me", id=d["id"], format="metadata")
                .execute()
            )
            msg_headers = {
                h["name"]: h["value"]
                for h in detail.get("message", {}).get("payload", {}).get("headers", [])
            }
            subject = _decode_mime_header(msg_headers.get("Subject", "(no subject)"))
            to = msg_headers.get("To", "")
            date = msg_headers.get("Date", "")
            lines.append(
                f"---\nDraft ID: {d['id']}\nTo: {to}\nDate: {date}\nSubject: {subject}"
            )
        logger.info(f"gmail list_drafts: {len(drafts)} drafts")
        return "\n".join(lines)

    def _action_draft_reply(
        gmail_id: str, body: str, cc: str, reply_all: bool, draft_id: str
    ) -> str:
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        svc = _build_service(_creds())

        # Get my own email address for reply-all exclusion
        my_email = svc.users().getProfile(userId="me").execute().get("emailAddress", "")

        orig = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=gmail_id,
                format="metadata",
                metadataHeaders=[
                    "Subject",
                    "From",
                    "To",
                    "Cc",
                    "Reply-To",
                    "Message-ID",
                ],
            )
            .execute()
        )
        headers = {
            h["name"]: h["value"] for h in orig.get("payload", {}).get("headers", [])
        }
        subject = headers.get("Subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        message_id = headers.get("Message-ID", "")
        thread_id = orig.get("threadId", "")

        # Reply-To overrides From if present
        reply_to = headers.get("Reply-To") or headers.get("From", "")

        if reply_all:
            # Collect all original recipients except ourselves
            all_recipients: list[str] = []
            for field in ("To", "Cc"):
                val = headers.get(field, "")
                if val:
                    all_recipients.extend(
                        a.strip() for a in val.split(",") if a.strip()
                    )
            # Also include the original sender
            if reply_to:
                all_recipients.append(reply_to)
            # Deduplicate and exclude self
            seen: set[str] = set()
            to_addrs: list[str] = []
            cc_addrs: list[str] = []
            first = True
            for addr in all_recipients:
                lower = addr.lower()
                if my_email.lower() in lower or lower in seen:
                    continue
                seen.add(lower)
                if first:
                    to_addrs.append(addr)
                    first = False
                else:
                    cc_addrs.append(addr)
            # Merge explicit cc param
            if cc:
                for addr in (a.strip() for a in cc.split(",") if a.strip()):
                    if addr.lower() not in seen:
                        cc_addrs.append(addr)
                        seen.add(addr.lower())
            to_str = ", ".join(to_addrs)
            cc_str = ", ".join(cc_addrs)
        else:
            to_str = reply_to
            cc_str = cc

        msg = MIMEMultipart()
        msg["To"] = to_str
        msg["Subject"] = subject
        if cc_str:
            msg["Cc"] = cc_str
        if message_id:
            msg["In-Reply-To"] = message_id
            msg["References"] = message_id
        msg.attach(MIMEText(body, "plain"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        message_body = {"raw": raw, "threadId": thread_id}

        if draft_id:
            draft = (
                svc.users()
                .drafts()
                .update(
                    userId="me",
                    id=draft_id,
                    body={"message": message_body},
                )
                .execute()
            )
            action_word = "updated"
        else:
            draft = (
                svc.users()
                .drafts()
                .create(
                    userId="me",
                    body={"message": message_body},
                )
                .execute()
            )
            action_word = "saved"

        logger.info(
            f"gmail draft_reply: draft_id={draft['id']} replying to {gmail_id} "
            f"reply_all={reply_all} updated={bool(draft_id)}"
        )
        result = f"Draft {action_word} (ID: {draft['id']})\nTo: {to_str}\n"
        if cc_str:
            result += f"Cc: {cc_str}\n"
        result += f"Subject: {subject}\n\nOpen Gmail Drafts to review and send."
        return result

    # --- Label actions ---

    def _action_list_labels() -> str:
        svc = _build_service(_creds())
        results = svc.users().labels().list(userId="me").execute()
        labels = results.get("labels", [])
        if not labels:
            return "No labels found."

        system_labels = []
        user_labels = []
        for lb in labels:
            entry = f"  {lb['name']} (id: {lb['id']})"
            if lb.get("type") == "system":
                system_labels.append(entry)
            else:
                user_labels.append(entry)

        parts = []
        if user_labels:
            parts.append("User labels:\n" + "\n".join(sorted(user_labels)))
        if system_labels:
            parts.append("System labels:\n" + "\n".join(sorted(system_labels)))
        logger.info(f"gmail list_labels: {len(labels)} labels")
        return "\n\n".join(parts)

    def _action_create_label(label_name: str) -> str:
        svc = _build_service(_creds())
        label = (
            svc.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": label_name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        logger.info(f"gmail create_label: {label['id']} name={label_name!r}")
        return f"Label created.\nName: {label['name']}\nID: {label['id']}"

    def _action_update_label(label_id: str, label_name: str) -> str:
        svc = _build_service(_creds())
        label = (
            svc.users()
            .labels()
            .update(
                userId="me",
                id=label_id,
                body={"name": label_name},
            )
            .execute()
        )
        logger.info(f"gmail update_label: {label_id} → {label_name!r}")
        return f"Label renamed.\nName: {label['name']}\nID: {label['id']}"

    def _action_delete_label(label_id: str) -> str:
        svc = _build_service(_creds())
        svc.users().labels().delete(userId="me", id=label_id).execute()
        logger.info(f"gmail delete_label: {label_id}")
        return f"Label deleted (ID: {label_id})."

    # --- Filter actions ---

    def _action_list_filters() -> str:
        svc = _build_service(_creds())
        results = svc.users().settings().filters().list(userId="me").execute()
        filters = results.get("filter", [])
        if not filters:
            return "No filters found."

        lines = [f"Found {len(filters)} filter(s):\n"]
        for f in filters:
            criteria = f.get("criteria", {})
            action = f.get("action", {})
            criteria_parts = []
            for key in ("from", "to", "subject", "query", "negatedQuery"):
                if criteria.get(key):
                    criteria_parts.append(f"{key}: {criteria[key]}")
            if criteria.get("hasAttachment"):
                criteria_parts.append("hasAttachment: true")

            action_parts = []
            if action.get("addLabelIds"):
                action_parts.append(f"addLabels: {action['addLabelIds']}")
            if action.get("removeLabelIds"):
                action_parts.append(f"removeLabels: {action['removeLabelIds']}")
            if action.get("forward"):
                action_parts.append(f"forward: {action['forward']}")

            lines.append(
                f"---\n"
                f"ID: {f['id']}\n"
                f"Criteria: {', '.join(criteria_parts) or '(none)'}\n"
                f"Actions: {', '.join(action_parts) or '(none)'}"
            )
        logger.info(f"gmail list_filters: {len(filters)} filters")
        return "\n".join(lines)

    def _action_create_filter(filter_criteria: str, filter_actions: str) -> str:
        svc = _build_service(_creds())
        criteria = json.loads(filter_criteria)
        actions = json.loads(filter_actions)

        # Convenience booleans → label ID translations
        add_labels = list(actions.get("addLabelIds", []))
        remove_labels = list(actions.get("removeLabelIds", []))
        if actions.get("markRead"):
            remove_labels.append("UNREAD")
        if actions.get("archive"):
            remove_labels.append("INBOX")
        if actions.get("star"):
            add_labels.append("STARRED")

        api_action = {}
        if add_labels:
            api_action["addLabelIds"] = add_labels
        if remove_labels:
            api_action["removeLabelIds"] = remove_labels
        if actions.get("forward"):
            api_action["forward"] = actions["forward"]

        body = {"criteria": criteria, "action": api_action}
        result = (
            svc.users()
            .settings()
            .filters()
            .create(
                userId="me",
                body=body,
            )
            .execute()
        )
        logger.info(f"gmail create_filter: {result['id']}")
        return (
            f"Filter created.\n"
            f"ID: {result['id']}\n"
            f"Criteria: {json.dumps(criteria)}\n"
            f"Actions: {json.dumps(api_action)}"
        )

    def _action_delete_filter(filter_id: str) -> str:
        svc = _build_service(_creds())
        svc.users().settings().filters().delete(
            userId="me",
            id=filter_id,
        ).execute()
        logger.info(f"gmail delete_filter: {filter_id}")
        return f"Filter deleted (ID: {filter_id})."

    @function_tool
    def gmail(
        action: str,
        query: str = "",
        gmail_id: str = "",
        download_attachments: bool = False,
        body: str = "",
        cc: str = "",
        reply_all: bool = False,
        draft_id: str = "",
        label_name: str = "",
        label_id: str = "",
        filter_id: str = "",
        filter_criteria: str = "",
        filter_actions: str = "",
        max_results: int = 10,
        page_token: str = "",
    ) -> str:
        """Gmail: search, read, draft replies, and manage labels & filters. Call with action='help' for detailed usage.

        Args:
            action:          One of: help, search, read, draft_reply, list_drafts, list_labels, create_label, update_label, delete_label, list_filters, create_filter, delete_filter.
            query:           (search) Gmail search query.
            gmail_id:        (read, draft_reply) Gmail message ID from search results.
            download_attachments: (read) If true, save all attachments to data/uploads/ and return their local paths.
            body:            (draft_reply) Plain-text reply body.
            cc:              (draft_reply) Comma-separated addresses to CC.
            reply_all:       (draft_reply) If true, reply to all original recipients (To + Cc), excluding yourself.
            draft_id:        (draft_reply) If provided, updates this existing draft instead of creating a new one.
            label_name:      (create_label, update_label) Label name.
            label_id:        (update_label, delete_label) Label ID.
            filter_id:       (delete_filter) Filter ID.
            filter_criteria: (create_filter) JSON criteria, e.g. '{"from": "news@example.com"}'.
            filter_actions:  (create_filter) JSON actions, e.g. '{"addLabelIds": ["Label_1"], "archive": true}'.
            max_results:     (search) Max results (up to 500). Default 10.
            page_token:      (search) Pagination token from a previous search to fetch the next page of results.
        """
        try:
            if action == "help":
                return _HELP_TEXT
            try:
                _creds()  # validate auth early, before dispatching
            except FileNotFoundError:
                return _missing_credentials_message(run_ctx)
            except OAuthRequired as e:
                return _oauth_required_message(e.url, run_ctx)
            if action == "search":
                if not query:
                    return "Missing required field: query."
                return _action_search(query, max_results, page_token)
            if action == "read":
                if not gmail_id:
                    return "Missing required field: gmail_id."
                return _action_read(gmail_id, download_attachments)
            if action == "draft_reply":
                if not gmail_id or not body:
                    return "Missing required fields: gmail_id, body."
                return _action_draft_reply(gmail_id, body, cc, reply_all, draft_id)
            if action == "list_drafts":
                return _action_list_drafts(max_results)
            if action == "list_labels":
                return _action_list_labels()
            if action == "create_label":
                if not label_name:
                    return "Missing required field: label_name."
                return _action_create_label(label_name)
            if action == "update_label":
                if not label_id or not label_name:
                    return "Missing required fields: label_id, label_name."
                return _action_update_label(label_id, label_name)
            if action == "delete_label":
                if not label_id:
                    return "Missing required field: label_id."
                return _action_delete_label(label_id)
            if action == "list_filters":
                return _action_list_filters()
            if action == "create_filter":
                if not filter_criteria or not filter_actions:
                    return "Missing required fields: filter_criteria, filter_actions."
                return _action_create_filter(filter_criteria, filter_actions)
            if action == "delete_filter":
                if not filter_id:
                    return "Missing required field: filter_id."
                return _action_delete_filter(filter_id)
            return f"Unknown action '{action}'. Use action='help' for usage."
        except json.JSONDecodeError as e:
            return f"Error: invalid JSON — {e}"
        except Exception as e:
            logger.error(f"gmail {action} failed: {e}", exc_info=True)
            return f"Error in gmail {action}: {e}"

    return gmail
