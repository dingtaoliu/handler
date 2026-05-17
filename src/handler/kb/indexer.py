"""Gmail email indexer — downloads and stores emails in the local KB database."""

import base64
import email
from email.header import decode_header
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import google.auth.exceptions
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .database import EmailDatabase

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

MAX_RETRIES = 5
RETRY_DELAYS = [1, 2, 4, 8, 16]

logger = logging.getLogger("handler.kb.indexer")


class GmailIndexer:
    """Downloads and indexes Gmail messages into the local emails DB."""

    def __init__(
        self,
        credentials_path: str,
        token_path: str,
        db_path: str,
    ):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.db = EmailDatabase(db_path)
        self.service = None
        self._authenticate()

    def _authenticate(self):
        creds = None
        if Path(self.token_path).exists():
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except google.auth.exceptions.RefreshError:
                    logger.warning("Token refresh failed, re-authenticating...")
                    Path(self.token_path).unlink(missing_ok=True)
                    creds = None
            if not creds or not creds.valid:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)

            Path(self.token_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.token_path).write_text(creds.to_json())

        self.service = build("gmail", "v1", credentials=creds)
        logger.info("Authenticated with Gmail API")

    def _retry_request(self, func, *args, **kwargs) -> Any:
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except HttpError as e:
                if e.resp.status in [429, 500, 502, 503]:
                    if attempt < MAX_RETRIES - 1:
                        delay = RETRY_DELAYS[attempt]
                        logger.warning(f"HTTP {e.resp.status}, retrying in {delay}s")
                        time.sleep(delay)
                    else:
                        raise
                else:
                    raise

    def _decode_mime_header(self, value: str) -> str:
        if not value:
            return ""
        parts = []
        for chunk, enc in decode_header(value):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(enc or "utf-8", errors="replace"))
            else:
                parts.append(chunk)
        return "".join(parts)

    def get_message_ids(
        self, year: int, month: Optional[int] = None, max_results: Optional[int] = None
    ) -> List[str]:
        if month:
            import calendar
            from datetime import date
            start = date(year, month, 1)
            last_day = calendar.monthrange(year, month)[1]
            end = date(year, month, last_day)
            query = f"after:{start.strftime('%Y/%m/%d')} before:{end.strftime('%Y/%m/%d')}"
        else:
            query = f"after:{year}/01/01 before:{year}/12/31"

        logger.info(f"Fetching message IDs: {query}")
        message_ids = []
        page_token = None

        while True:
            results = self._retry_request(
                self.service.users().messages().list,
                userId="me",
                q=query,
                maxResults=min(500, max_results - len(message_ids)) if max_results else 500,
                pageToken=page_token,
            ).execute()

            message_ids.extend(msg["id"] for msg in results.get("messages", []))
            page_token = results.get("nextPageToken")

            if not page_token or (max_results and len(message_ids) >= max_results):
                break

        return message_ids[:max_results] if max_results else message_ids

    def download_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self._retry_request(
                self.service.users().messages().get,
                userId="me",
                id=message_id,
                format="raw",
            ).execute()
        except HttpError as e:
            logger.error(f"Error downloading {message_id}: {e}")
            return None

    def parse_message(self, raw_message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            raw_email = base64.urlsafe_b64decode(raw_message["raw"])
            msg = email.message_from_bytes(raw_email)

            from_header = msg.get("From", "")
            parsed = email.utils.parseaddr(from_header)
            from_email = parsed[1]
            from_name = parsed[0] or None

            date_str = msg.get("Date", "")
            date_timestamp = None
            try:
                if date_str:
                    from email.utils import parsedate_to_datetime
                    date_timestamp = int(parsedate_to_datetime(date_str).timestamp())
            except Exception:
                date_timestamp = int(raw_message.get("internalDate", 0)) // 1000

            plain, html = self._extract_body(msg)
            attachments = self._extract_attachments(msg, raw_message)

            to_raw = msg.get("To", "")
            cc_raw = msg.get("Cc", "")
            to_emails = [a[1] for a in email.utils.getaddresses([to_raw]) if a[1]]
            cc_emails = [a[1] for a in email.utils.getaddresses([cc_raw]) if a[1]]

            return {
                "gmail_id": raw_message["id"],
                "thread_id": raw_message.get("threadId"),
                "subject": self._decode_mime_header(msg.get("Subject", "")),
                "from_email": from_email,
                "from_name": from_name,
                "to_emails": to_emails,
                "cc_emails": cc_emails,
                "date": date_str,
                "date_timestamp": date_timestamp,
                "body_plain": plain,
                "body_html": html,
                "labels": raw_message.get("labelIds", []),
                "has_attachments": len(attachments) > 0,
                "attachment_count": len(attachments),
                "attachment_info": attachments or None,
                "size_bytes": raw_message.get("sizeEstimate", 0),
            }
        except Exception as e:
            logger.error(f"Error parsing {raw_message.get('id')}: {e}")
            return None

    def _extract_body(self, msg) -> Tuple[Optional[str], Optional[str]]:
        plain = html = None
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain" and not plain:
                    try:
                        plain = part.get_payload(decode=True).decode(
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
                        plain = decoded
                    elif ct == "text/html":
                        html = decoded
            except Exception:
                pass
        return plain, html

    def _extract_attachments(self, msg, raw_message) -> List[Dict[str, Any]]:
        attachments = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_disposition() == "attachment":
                    filename = part.get_filename()
                    if filename:
                        attachments.append({
                            "name": filename,
                            "size": len(part.get_payload(decode=False)),
                            "mimeType": part.get_content_type(),
                            "link": f"https://mail.google.com/mail/u/0/#inbox/{raw_message['id']}",
                        })
        return attachments

    def index_messages(
        self,
        year: int,
        month: Optional[int] = None,
        max_emails: Optional[int] = None,
        overwrite: bool = False,
        progress_callback=None,
    ) -> Dict[str, int]:
        logger.info(f"Starting indexer: year={year} month={month}")
        stats = {"downloaded": 0, "skipped": 0, "errors": 0}

        message_ids = self.get_message_ids(year, month, max_emails)
        total = len(message_ids)
        if not total:
            return stats

        self.db.update_progress(year, month, total, 0, completed=False)

        for idx, message_id in enumerate(message_ids, 1):
            try:
                if not overwrite and self.db.message_exists(message_id):
                    stats["skipped"] += 1
                    if progress_callback:
                        progress_callback(idx, total, {"gmail_id": message_id, "skipped": True})
                    continue

                raw = self.download_message(message_id)
                if not raw:
                    stats["errors"] += 1
                    continue

                data = self.parse_message(raw)
                if not data:
                    stats["errors"] += 1
                    continue

                if overwrite and self.db.message_exists(message_id):
                    self.db.update_message(message_id, data)
                else:
                    self.db.insert_message(data)

                stats["downloaded"] += 1
                if progress_callback:
                    progress_callback(idx, total, data)

                if idx % 100 == 0:
                    self.db.update_progress(year, month, total, idx, message_id, completed=False)

            except Exception as e:
                logger.error(f"Error processing {message_id}: {e}")
                stats["errors"] += 1

        self.db.update_progress(year, month, total, total, completed=True)
        logger.info(f"Indexing done: {stats}")
        return stats

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
