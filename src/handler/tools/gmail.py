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
import logging
import os
import re
from email.header import decode_header
from email.message import Message as EmailMessage
from pathlib import Path

from agents import function_tool

import json

from ..paths import DATA_DIR as _DATA_DIR, GMAIL_UPLOAD_DIR

logger = logging.getLogger("handler.tools.gmail")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]
_CREDENTIALS_PATH = _DATA_DIR / "credentials" / "desktop.json"
_TOKEN_PATH = _DATA_DIR / "credentials" / "token.json"

_HELP_TEXT = """\
gmail — search, read, draft replies, and manage labels & filters.

Actions:
  search        — Search Gmail using Gmail query syntax.
                  Params: query (required), max_results (default 10).
                  Query examples: "from:bank subject:statement", "after:2025/01/01 is:unread",
                  "has:attachment filename:pdf", "in:inbox from:github".
    read          — Read a specific email by its Gmail ID (from search results).
                                    Params: gmail_id (required), download_attachments (optional, default false).
                                    If download_attachments is true, attachments are saved to data/uploads/gmail/ and local paths are returned.
  draft_reply   — Draft a reply to an email. Saved in Gmail Drafts — nothing is sent.
                  Params: gmail_id (required), body (required).
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
    text = re.sub(
        r"(?i)\n\s*sent from my (iphone|android|ipad|mobile).*",
        "",
        text,
        flags=re.DOTALL,
    )

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


def gmail_tool():
    """Create a single gmail tool. Authenticates on first call.

    Returns a single @function_tool.
    Raises FileNotFoundError if credentials are not set up.
    """
    creds = _get_credentials()

    def _action_search(query: str, max_results: int) -> str:
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

        logger.info(f"gmail search: query={query!r} results={len(messages)}")
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

    def _action_draft_reply(gmail_id: str, body: str) -> str:
        from email.mime.text import MIMEText

        svc = _build_service(creds)

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
            h["name"]: h["value"] for h in orig.get("payload", {}).get("headers", [])
        }
        subject = headers.get("Subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        reply_to = headers.get("From", "")
        message_id = headers.get("Message-ID", "")
        thread_id = orig.get("threadId", "")

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

        logger.info(f"gmail draft_reply: draft_id={draft['id']} replying to {gmail_id}")
        return (
            f"Draft saved (ID: {draft['id']})\n"
            f"To: {reply_to}\n"
            f"Subject: {subject}\n\n"
            f"Open Gmail Drafts to review and send."
        )

    # --- Label actions ---

    def _action_list_labels() -> str:
        svc = _build_service(creds)
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
        svc = _build_service(creds)
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
        svc = _build_service(creds)
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
        svc = _build_service(creds)
        svc.users().labels().delete(userId="me", id=label_id).execute()
        logger.info(f"gmail delete_label: {label_id}")
        return f"Label deleted (ID: {label_id})."

    # --- Filter actions ---

    def _action_list_filters() -> str:
        svc = _build_service(creds)
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
        svc = _build_service(creds)
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
        svc = _build_service(creds)
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
        label_name: str = "",
        label_id: str = "",
        filter_id: str = "",
        filter_criteria: str = "",
        filter_actions: str = "",
        max_results: int = 10,
    ) -> str:
        """Gmail: search, read, draft replies, and manage labels & filters. Call with action='help' for detailed usage.

        Args:
            action:          One of: help, search, read, draft_reply, list_labels, create_label, update_label, delete_label, list_filters, create_filter, delete_filter.
            query:           (search) Gmail search query.
            gmail_id:        (read, draft_reply) Gmail message ID from search results.
            download_attachments: (read) If true, save all attachments to data/uploads/ and return their local paths.
            body:            (draft_reply) Plain-text reply body.
            label_name:      (create_label, update_label) Label name.
            label_id:        (update_label, delete_label) Label ID.
            filter_id:       (delete_filter) Filter ID.
            filter_criteria: (create_filter) JSON criteria, e.g. '{"from": "news@example.com"}'.
            filter_actions:  (create_filter) JSON actions, e.g. '{"addLabelIds": ["Label_1"], "archive": true}'.
            max_results:     (search) Max results. Default 10.
        """
        try:
            if action == "help":
                return _HELP_TEXT
            if action == "search":
                if not query:
                    return "Missing required field: query."
                return _action_search(query, max_results)
            if action == "read":
                if not gmail_id:
                    return "Missing required field: gmail_id."
                return _action_read(gmail_id, download_attachments)
            if action == "draft_reply":
                if not gmail_id or not body:
                    return "Missing required fields: gmail_id, body."
                return _action_draft_reply(gmail_id, body)
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
