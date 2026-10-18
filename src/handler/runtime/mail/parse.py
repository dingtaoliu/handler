"""Parse a raw (usually forwarded) email with the stdlib `email` package.

Two forwarding styles are handled:
- inline: "---------- Forwarded message ---------" (Gmail), "-----Original
  Message-----" / "From: … Sent: …" (Outlook), "Begin forwarded message:"
  (Apple Mail), and common Chinese client markers. The header block after the
  marker gives the original sender, date and subject;
- as attachment: `message/rfc822` parts. Each nested message is parsed the
  same way (to MAX_DEPTH), and its own attachments are collected too.

The top-level From is the forwarder; `originals` lists what was forwarded.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from ..uploads import sanitize_filename

MAX_DEPTH = 5  # nested message/rfc822 levels
MAX_ATTACHMENTS = 100


@dataclass
class Address:
    name: str = ""
    address: str = ""

    def __str__(self) -> str:
        if self.name and self.address:
            return f"{self.name} <{self.address}>"
        return self.address or self.name

    def as_dict(self) -> dict:
        return {"name": self.name, "address": self.address}


@dataclass
class Attachment:
    filename: str  # sanitized, unique within the message
    content_type: str
    data: bytes
    origin: str = ""  # "" for the top message, "forwarded-1" for a nested one, ...

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class Original:
    """A forwarded message, from an inline block or a message/rfc822 part."""

    source: str  # "inline" | "attachment"
    sender: Address
    date: str = ""
    date_iso: str | None = None
    subject: str = ""
    to: str = ""
    message_id: str = ""
    body_text: str = ""
    eml_name: str | None = None  # attachment file holding the nested message

    def as_dict(self) -> dict:
        return {"source": self.source, "from": self.sender.as_dict(), "date": self.date,
                "date_iso": self.date_iso, "subject": self.subject, "to": self.to,
                "message_id": self.message_id, "eml": self.eml_name}


@dataclass
class ParsedEmail:
    message_id: str
    root_message_id: str
    sender: Address  # the forwarder (top-level From)
    to: list[Address]
    cc: list[Address]
    subject: str
    date: str
    date_iso: str | None
    body_text: str  # the forwarder's whole body as text
    note: str  # text the forwarder wrote above an inline forward ("" if none)
    originals: list[Original] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # attachments not kept, and why

    @property
    def original(self) -> Original | None:
        return self.originals[0] if self.originals else None


# ── helpers ────────────────────────────────────────────────────────────────

_MSGID = re.compile(r"<([^<>\s]+)>")


def _ids(value: str | None) -> list[str]:
    if not value:
        return []
    found = _MSGID.findall(str(value))
    if not found and str(value).strip():
        found = [str(value).strip().strip("<>")]
    return found


def _addr(value: str | None) -> Address:
    name, address = parseaddr(str(value or ""))
    return Address(name.strip(), address.strip())


def _addrs(value: str | None) -> list[Address]:
    return [Address(n.strip(), a.strip()) for n, a in getaddresses([str(value or "")]) if a]


def _iso(date: str) -> str | None:
    if not date:
        return None
    try:
        parsed = parsedate_to_datetime(date)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            from dateutil import parser as dateparser

            cleaned = re.sub(r"\s+at\s+", " ", date)
            cleaned = re.sub(r"[（(][^)）]*[)）]", "", cleaned)
            parsed = dateparser.parse(cleaned, fuzzy=True)
        except (ImportError, ValueError, OverflowError, TypeError):
            return None
    if parsed.tzinfo is None:
        return parsed.isoformat()
    return parsed.astimezone(timezone.utc).isoformat()


def html_to_text(html: str) -> str:
    try:
        import html2text

        converter = html2text.HTML2Text()
        converter.body_width = 0
        converter.ignore_images = True
        converter.ignore_emphasis = True
        return converter.handle(html).strip()
    except Exception:  # noqa: BLE001 - fall back to tag stripping
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        import html as htmllib

        return htmllib.unescape(text).strip()


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()  # type: ignore[attr-defined]
    except (LookupError, ValueError, AttributeError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) \
            else str(payload)
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    return str(content)


def body_text(msg: EmailMessage) -> str:
    """Plain-text body, or the HTML body converted to text."""
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        text = _part_text(plain)
        if text.strip():
            return text.replace("\r\n", "\n").strip()
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        return html_to_text(_part_text(html)).replace("\r\n", "\n")
    return ""


# ── inline forwards ────────────────────────────────────────────────────────

_MARKERS = re.compile(
    r"^[ \t>]*(?:"
    r"-{2,}\s*(?:Forwarded message|Original Message|Forwarded Message|"
    r"转发的邮件|轉寄的郵件|轉發的郵件|原始邮件|原始郵件|原始信件)\s*-{2,}"
    r"|Begin forwarded message:"
    r"|_{8,}"
    r")[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_KEYS = {
    "from": ("from", "发件人", "發件人", "寄件者", "寄件人", "差出人", "von", "de"),
    "date": ("date", "sent", "日期", "发送时间", "發送時間", "寄件日期", "时间", "時間",
             "送信日時", "datum", "envoyé"),
    "subject": ("subject", "主题", "主題", "主旨", "件名", "betreff", "objet"),
    "to": ("to", "收件人", "收件者", "宛先", "an", "à"),
}
_KEY_LOOKUP = {alias: key for key, aliases in _KEYS.items() for alias in aliases}
_HEADER_LINE = re.compile(r"^[ \t>]*\**([^\s:：*][^:：]{0,20}?)\**\s*[:：]\s*(.*)$")


def _header_block(lines: list[str], start: int) -> tuple[dict[str, str], int] | None:
    """Parse "Key: value" lines from `start`; needs From plus Date or Subject."""
    found: dict[str, str] = {}
    i = start
    while i < len(lines) and not lines[i].strip(" \t>"):
        i += 1
    last = None
    while i < len(lines):
        line = lines[i]
        if not line.strip(" \t>"):
            break
        match = _HEADER_LINE.match(line)
        key = _KEY_LOOKUP.get(match.group(1).strip().lower()) if match else None
        if key:
            found.setdefault(key, match.group(2).strip())
            last = key
        elif last and line[:1] in (" ", "\t"):
            found[last] += " " + line.strip()
        elif found:
            break
        else:
            return None
        i += 1
    if "from" in found and ("date" in found or "subject" in found):
        return found, i
    return None


def _clean_value(value: str) -> str:
    value = re.sub(r"\*\*|__", "", value)
    value = re.sub(r"\[([^\]]+)\]\(mailto:[^)]+\)", r"\1", value)  # html2text links
    value = value.replace("mailto:", "")
    return value.strip()


def split_inline_forward(text: str) -> tuple[str, Original] | None:
    """(note above the forward, the forwarded message) or None."""
    lines = text.split("\n")
    candidates = [text[: m.start()].count("\n") for m in _MARKERS.finditer(text)]
    # Outlook without a marker line: a bare "From: … / Sent: …" block.
    for idx, line in enumerate(lines):
        if _HEADER_LINE.match(line) and _KEY_LOOKUP.get(
                _HEADER_LINE.match(line).group(1).strip().lower()) == "from":
            candidates.append(idx - 1)
    for marker_line in sorted(set(candidates)):
        parsed = _header_block(lines, marker_line + 1)
        if parsed is None:
            continue
        fields, end = parsed
        note = "\n".join(lines[: max(marker_line, 0)]).strip()
        body_lines = [re.sub(r"^> ?", "", line) for line in lines[end:]]
        sender = _addr(_clean_value(fields["from"]))
        if not sender.address:
            match = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", fields["from"])
            sender = Address(_clean_value(fields["from"]).split("<")[0].strip(" \"'"),
                             match.group(0) if match else "")
        date = _clean_value(fields.get("date", ""))
        return note, Original(
            source="inline", sender=sender, date=date, date_iso=_iso(date),
            subject=_clean_value(fields.get("subject", "")),
            to=_clean_value(fields.get("to", "")),
            body_text="\n".join(body_lines).strip(),
        )
    return None


# ── walking parts ──────────────────────────────────────────────────────────


class _Collector:
    def __init__(self) -> None:
        self.attachments: list[Attachment] = []
        self.originals: list[Original] = []
        self.skipped: list[str] = []
        self._names: set[str] = set()

    def unique(self, name: str) -> str:
        base = sanitize_filename(name)
        candidate, n = base, 2
        stem, dot, ext = base.rpartition(".")
        while candidate.lower() in self._names:
            candidate = f"{stem}-{n}.{ext}" if dot and stem else f"{base}-{n}"
            n += 1
        self._names.add(candidate.lower())
        return candidate

    def add(self, name: str, content_type: str, data: bytes, origin: str) -> str | None:
        if len(self.attachments) >= MAX_ATTACHMENTS:
            self.skipped.append(f"{name}: more than {MAX_ATTACHMENTS} attachments")
            return None
        filename = self.unique(f"{origin}-{name}" if origin else name)
        self.attachments.append(Attachment(filename, content_type, data, origin))
        return filename


def _default_name(part: Message, index: int) -> str:
    ext = mimetypes.guess_extension(part.get_content_type() or "") or ".bin"
    return f"part-{index}{ext}"


def _nested_message(part: Message) -> EmailMessage | None:
    payload = part.get_payload()
    if isinstance(payload, list) and payload:
        nested = payload[0]
    else:
        nested = payload
    if isinstance(nested, Message):
        return nested  # type: ignore[return-value]
    if isinstance(nested, (str, bytes)):
        data = nested.encode("utf-8", "surrogateescape") if isinstance(nested, str) else nested
        return BytesParser(policy=policy.default).parsebytes(data)  # type: ignore[return-value]
    return None


def _leaves(msg: Message):
    """Parts below `msg`, not descending into nested message/rfc822 parts."""
    stack = list(reversed(msg.get_payload())) if msg.is_multipart() else []
    while stack:
        part = stack.pop()
        if not isinstance(part, Message):
            continue
        if part.get_content_type() != "message/rfc822" and part.is_multipart():
            stack.extend(reversed(part.get_payload()))  # type: ignore[arg-type]
            continue
        yield part


def _walk(msg: EmailMessage, collector: _Collector, origin: str, depth: int) -> None:
    body_parts = set()
    for preference in (("plain",), ("html",)):
        found = msg.get_body(preferencelist=preference)
        if found is not None and found is not msg:
            body_parts.add(id(found))
    for index, part in enumerate(_leaves(msg), start=1):
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            nested = _nested_message(part)
            if nested is None:
                continue
            label = f"forwarded-{len(collector.originals) + 1}"
            eml_name = collector.add(
                f"{label}.eml", "message/rfc822",
                nested.as_bytes(policy=policy.default.clone(linesep="\r\n")), "")
            date = str(nested.get("date", "") or "")
            collector.originals.append(Original(
                source="attachment", sender=_addr(nested.get("from")),
                date=date, date_iso=_iso(date),
                subject=str(nested.get("subject", "") or ""),
                to=str(nested.get("to", "") or ""),
                message_id=(_ids(nested.get("message-id")) or [""])[0],
                body_text=body_text(nested), eml_name=eml_name))
            if depth < MAX_DEPTH:
                _walk(nested, collector, label, depth + 1)
            else:
                collector.skipped.append(f"{label}: nested deeper than {MAX_DEPTH}")
            continue
        if id(part) in body_parts:
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if not filename and disposition != "attachment" and ctype.startswith("text/"):
            continue  # alternative bodies and inline text that aren't files
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes):
            continue
        collector.add(filename or _default_name(part, index), ctype, data, origin)


def parse_email(raw: bytes) -> ParsedEmail:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[assignment]
    message_id = (_ids(msg.get("message-id")) or [""])[0]
    if not message_id:
        message_id = "sha256-" + hashlib.sha256(raw).hexdigest()[:32] + "@handler.invalid"
    references = _ids(msg.get("references"))
    in_reply_to = _ids(msg.get("in-reply-to"))
    root = (references or in_reply_to or [message_id])[0]
    date = str(msg.get("date", "") or "")
    collector = _Collector()
    _walk(msg, collector, "", 1)
    text = body_text(msg)
    note = ""
    inline = split_inline_forward(text)
    originals = list(collector.originals)
    if inline is not None:
        note, original = inline
        originals.insert(0, original)
    elif originals:
        note = text  # forwarded as attachment: the whole body is the forwarder's note
    return ParsedEmail(
        message_id=message_id, root_message_id=root, sender=_addr(msg.get("from")),
        to=_addrs(msg.get("to")), cc=_addrs(msg.get("cc")),
        subject=str(msg.get("subject", "") or ""), date=date, date_iso=_iso(date),
        body_text=text, note=note,
        originals=originals, attachments=collector.attachments, skipped=collector.skipped,
    )


def folder_name(parsed: ParsedEmail, received: datetime | None = None) -> str:
    """inbox/<date>-<slug>: deterministic per Message-ID, so retries reuse it."""
    when = None
    if parsed.date_iso:
        try:
            when = datetime.fromisoformat(parsed.date_iso)
        except ValueError:
            when = None
    when = when or received or datetime.now(timezone.utc)
    subject = re.sub(r"^\s*((re|fw|fwd|aw|wg|转发|轉寄|回复|回覆)\s*[:：]\s*)+", "",
                     parsed.subject, flags=re.IGNORECASE)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", subject).strip("-").lower()[:40].strip("-") or "email"
    digest = hashlib.sha256(parsed.message_id.encode()).hexdigest()[:8]
    return f"{when:%Y-%m-%d}-{slug}-{digest}"
