"""Parse one raw message from a mailbox, robust to Chinese mail clients.

Chinese providers and clients routinely:
- label GBK text as `gb2312` (GBK is a superset; strict gb2312 then fails),
  so every GB charset is decoded as GB18030, which contains both;
- send RFC 2047 encoded words in headers and in attachment filenames
  (`filename="=?GB2312?B?...?="`, which RFC 2231 says not to do);
- send raw 8-bit header bytes with no declared charset.

Everything is decoded with a fallback chain (declared charset, UTF-8,
GB18030, Big5-HKSCS) before giving up with replacement characters, so a bad
label never loses a message.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.header import Header, decode_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from ...mail.parse import html_to_text
from ...uploads import sanitize_filename

MAX_BODY_CHARS = 200_000  # stored per message
MAX_ATTACHMENTS = 100

# Charset labels that mean "some GB encoding": decode them all as GB18030.
_CHARSET_ALIASES = {
    "gb2312": "gb18030", "gb_2312-80": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030",
    "cp936": "gb18030", "ms936": "gb18030", "euc-cn": "gb18030", "csgb2312": "gb18030",
    "x-euc-cn": "gb18030", "chinese": "gb18030", "gb18030": "gb18030",
    "big5": "big5hkscs", "x-big5": "big5hkscs", "cn-big5": "big5hkscs",
    "ks_c_5601-1987": "cp949", "utf8": "utf-8", "unicode-1-1-utf-7": "utf-7",
}
_UNKNOWN = {"unknown-8bit", "x-unknown", "unknown", "default", "x-user-defined", ""}
_FALLBACKS = ("utf-8", "gb18030", "big5hkscs")
_SURROGATES = re.compile("[\udc80-\udcff]")
_MSGID = re.compile(r"<([^<>\s]+)>")
_REPLY_PREFIX = re.compile(
    r"^\s*(?:\[[^\]]{0,30}\]\s*)?"
    r"(?:re|fw|fwd|aw|wg|sv|vs|tr|rif|antw|答复|回复|回覆|转发|轉寄|轉發)"
    r"\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*", re.IGNORECASE)


def charset_name(label: str | None) -> str | None:
    """The codec to use for a declared charset label, or None when unusable."""
    name = (label or "").strip().strip('"').lower()
    if name in _UNKNOWN:
        return None
    return _CHARSET_ALIASES.get(name, name)


def decode_bytes(data: bytes, label: str | None = None) -> str:
    """Decode with the declared charset, then UTF-8, GB18030, Big5; never raises."""
    declared = charset_name(label)
    tried = []
    for codec in ((declared,) if declared else ()) + _FALLBACKS:
        if codec in tried:
            continue
        tried.append(codec)
        try:
            return data.decode(codec)
        except (LookupError, UnicodeDecodeError):
            continue
    try:
        return data.decode(declared or "utf-8", errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _fix_8bit(value: object) -> str:
    """Header text with undeclared 8-bit bytes decoded.

    The compat32 parser hands such headers back as `Header` objects holding
    `unknown-8bit` chunks (or as surrogate-escaped strings).
    """
    if isinstance(value, Header):
        out = []
        for chunk, charset in value._chunks:  # (str with surrogates, Charset)
            if isinstance(chunk, str) and _SURROGATES.search(chunk):
                out.append(decode_bytes(chunk.encode("ascii", "surrogateescape"),
                                        None if str(charset) == "unknown-8bit" else str(charset)))
            else:
                out.append(str(chunk))
        return "".join(out)
    value = str(value)
    if not _SURROGATES.search(value):
        return value
    return decode_bytes(value.encode("utf-8", "surrogateescape"))


def decode_header_value(value: object) -> str:
    """A header's text: RFC 2047 encoded words and raw 8-bit bytes decoded."""
    if value is None:
        return ""
    text = _fix_8bit(value)
    try:
        parts = decode_header(text)
    except Exception:  # noqa: BLE001 - malformed encoded words: keep the raw text
        return " ".join(text.split())
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            # Unencoded segments come back as raw-unicode-escape bytes.
            out.append(chunk.decode("raw-unicode-escape", errors="replace")
                       if charset is None else decode_bytes(chunk, charset))
        else:
            out.append(chunk)
    return " ".join("".join(out).split())


def message_ids(value: object) -> list[str]:
    text = decode_header_value(value)
    found = _MSGID.findall(text)
    if not found and text.strip() and " " not in text.strip():
        found = [text.strip().strip("<>")]
    return found


def normalize_subject(subject: str) -> str:
    """Subject without Re:/Fwd:/回复: prefixes, lowercased, spaces collapsed."""
    text = subject or ""
    while True:
        stripped = _REPLY_PREFIX.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return " ".join(text.split()).lower()


def is_reply_subject(subject: str) -> bool:
    return bool(_REPLY_PREFIX.match(subject or ""))


@dataclass
class Address:
    name: str
    address: str

    def as_dict(self) -> dict:
        return {"name": self.name, "address": self.address}

    def __str__(self) -> str:
        return f"{self.name} <{self.address}>" if self.name else self.address


@dataclass
class Attachment:
    filename: str  # sanitized, unique within the message
    content_type: str
    data: bytes
    inline: bool = False

    @property
    def size(self) -> int:
        return len(self.data)

    def meta(self, index: int) -> dict:
        return {"index": index, "filename": self.filename, "content_type": self.content_type,
                "size": self.size, "inline": self.inline}


@dataclass
class MailMessage:
    message_id: str
    in_reply_to: list[str]
    references: list[str]
    subject: str
    sender: Address
    reply_to: list[Address]
    to: list[Address]
    cc: list[Address]
    date_iso: str | None
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)
    body_truncated: bool = False
    headers_only: bool = False

    @property
    def parent_ids(self) -> list[str]:
        """Ids this message points at, nearest first (In-Reply-To, then References)."""
        ordered = list(self.in_reply_to) + list(reversed(self.references))
        return list(dict.fromkeys(i for i in ordered if i and i != self.message_id))

    def participants(self) -> list[str]:
        found = [a.address.lower() for a in (self.sender, *self.to, *self.cc, *self.reply_to)
                 if a.address]
        return list(dict.fromkeys(found))


def _addresses(values: list[str] | None) -> list[Address]:
    if not values:
        return []
    fixed = [_fix_8bit(v) for v in values]
    out = []
    for name, address in getaddresses(fixed):
        address = address.strip()
        if not address or "@" not in address:
            continue
        out.append(Address(decode_header_value(name).strip(" \"'"), address))
    return out


def _iso(date: str | None) -> str | None:
    if not date:
        return None
    try:
        parsed = parsedate_to_datetime(date)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        payload = str(part.get_payload() or "").encode("utf-8", "surrogateescape")
    return decode_bytes(payload, part.get_content_charset()).replace("\r\n", "\n")


def _filename(part: Message) -> str | None:
    raw = part.get_filename() or part.get_param("name")
    if isinstance(raw, tuple):  # RFC 2231 (charset, language, value)
        try:
            raw = raw[2].encode("latin-1", "surrogateescape").decode(
                charset_name(raw[0]) or "utf-8", errors="replace")
        except (LookupError, UnicodeError):
            raw = raw[2]
    return decode_header_value(raw) if raw else None


def _unique(name: str, used: set[str]) -> str:
    base = sanitize_filename(name, default="attachment")
    candidate, n = base, 2
    stem, dot, ext = base.rpartition(".")
    while candidate.lower() in used:
        candidate = f"{stem}-{n}.{ext}" if dot and stem else f"{base}-{n}"
        n += 1
    used.add(candidate.lower())
    return candidate


def _walk(msg: Message):
    """Leaf parts, depth first, without descending into attached messages."""
    if not msg.is_multipart():
        yield msg
        return
    for part in msg.get_payload():
        if not isinstance(part, Message):
            continue
        if part.is_multipart() and part.get_content_type() != "message/rfc822":
            yield from _walk(part)
        else:
            yield part


def parse_message(raw: bytes, *, headers_only: bool = False) -> MailMessage:
    """Parse a raw RFC 822 message. With `headers_only`, the body is left empty."""
    msg = BytesParser(policy=policy.compat32).parsebytes(raw, headersonly=headers_only)
    ids = message_ids(msg.get("message-id"))
    message_id = ids[0] if ids else (
        "sha256-" + hashlib.sha256(raw).hexdigest()[:32] + "@handler.invalid")
    plain: list[str] = []
    html: list[str] = []
    attachments: list[Attachment] = []
    used: set[str] = set()
    if not headers_only:
        for index, part in enumerate(_walk(msg), start=1):
            ctype = part.get_content_type()
            disposition = (part.get("content-disposition") or "").split(";")[0].strip().lower()
            filename = _filename(part)
            if ctype == "message/rfc822":
                inner = part.get_payload()
                inner = inner[0] if isinstance(inner, list) and inner else inner
                data = inner.as_bytes() if isinstance(inner, Message) else b""
                filename = filename or f"attached-message-{index}.eml"
            elif not filename and disposition != "attachment" and ctype in (
                    "text/plain", "text/html"):
                (plain if ctype == "text/plain" else html).append(_part_text(part))
                continue
            else:
                data = part.get_payload(decode=True)
                if not isinstance(data, bytes):
                    continue
                filename = filename or f"part-{index}.bin"
            if len(attachments) >= MAX_ATTACHMENTS:
                continue
            attachments.append(Attachment(
                _unique(filename, used), ctype, data,
                inline=disposition == "inline" or bool(part.get("content-id"))))
    if any(p.strip() for p in plain):
        body = "\n\n".join(p.strip() for p in plain if p.strip())
    else:
        body = "\n\n".join(html_to_text(h) for h in html if h.strip())
    truncated = len(body) > MAX_BODY_CHARS
    senders = _addresses(msg.get_all("from"))
    return MailMessage(
        message_id=message_id,
        in_reply_to=message_ids(msg.get("in-reply-to")),
        references=message_ids(msg.get("references")),
        subject=decode_header_value(msg.get("subject")),
        sender=senders[0] if senders else Address("", ""),
        reply_to=_addresses(msg.get_all("reply-to")),
        to=_addresses(msg.get_all("to")),
        cc=_addresses(msg.get_all("cc")),
        date_iso=_iso(decode_header_value(msg.get("date"))),
        body_text=body[:MAX_BODY_CHARS],
        attachments=attachments,
        body_truncated=truncated,
        headers_only=headers_only,
    )


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
