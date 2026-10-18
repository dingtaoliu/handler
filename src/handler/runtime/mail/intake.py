"""Email intake rules: sender gate, thread key, acting user, files and turn text."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .dkim import DkimResult, TxtResolver, verify_message
from .parse import Address, ParsedEmail, folder_name

logger = logging.getLogger("handler.runtime.mail")

MAX_BODY_CHARS = 40_000  # in the event payload; the full text is saved as body.txt
MAX_EXTRACT_BYTES = 15 * 1024 * 1024
EXTRACT_TIMEOUT_SECONDS = 20.0
EXTRACT_BUDGET_SECONDS = 60.0
_EXTRACTABLE = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".csv", ".txt",
                ".md", ".html", ".htm", ".json", ".xml"}
# Two-label public suffixes that must never count as an aligned signing domain.
_SHARED_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.jp", "ne.jp",
    "or.jp", "com.tw", "net.tw", "org.tw", "idv.tw", "com.cn", "net.cn", "org.cn",
    "com.hk", "com.sg", "com.my", "co.kr", "co.in", "co.nz", "com.br", "com.mx",
    "co.za", "com.tr", "com.vn", "co.id", "com.ph", "co.th", "in.th",
}


# ── thread key ─────────────────────────────────────────────────────────────


def thread_key(parsed: ParsedEmail) -> str:
    """email:<root message-id> (References' first id, else In-Reply-To, else its own)."""
    root = parsed.root_message_id.strip()
    if len(root) > 200:
        root = "sha256-" + hashlib.sha256(root.encode()).hexdigest()[:32]
    return f"email:{root}"


def idempotency_key(parsed: ParsedEmail) -> str:
    return f"email:{parsed.message_id}"


# ── sender gate ────────────────────────────────────────────────────────────


def _domain(address: str) -> str:
    return address.rpartition("@")[2].lower().rstrip(".")


def normalize_address(address: str) -> str:
    """Lowercase, with any +tag removed from the local part."""
    local, at, domain = address.strip().lower().rpartition("@")
    if not at:
        return address.strip().lower()
    return f"{local.split('+', 1)[0]}@{domain}"


def sender_allowed(address: str, allowed: list[str]) -> bool:
    """Exact address (ignoring +tags) or an @domain / domain entry; empty list → False."""
    if not address or "@" not in address:
        return False
    normalized = normalize_address(address)
    domain = _domain(normalized)
    for entry in allowed:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry.startswith("@"):
            if domain == entry[1:]:
                return True
        elif "@" in entry:
            if normalized == normalize_address(entry):
                return True
        elif domain == entry:
            return True
    return False


def aligned(signing_domain: str, identity_domain: str) -> bool:
    """Relaxed alignment: same domain, or one is a parent of the other.

    Without a public suffix list, a parent must have at least two labels and
    not be a known shared suffix (co.uk, com.tw, ...).
    """
    d, f = signing_domain.lower().rstrip("."), identity_domain.lower().rstrip(".")
    if not d or not f:
        return False
    if d == f:
        return True
    parent, child = (d, f) if f.endswith("." + d) else (f, d) if d.endswith("." + f) else ("", "")
    if not parent:
        return False
    return parent.count(".") >= 1 and parent not in _SHARED_SUFFIXES


@dataclass
class AuthVerdict:
    passed: bool
    method: str | None = None  # "dkim" when passed
    domain: str | None = None  # the aligned signing domain
    dkim: list[DkimResult] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict:
        return {"passed": self.passed, "method": self.method, "domain": self.domain,
                "detail": self.detail,
                "dkim": [{"domain": r.domain, "selector": r.selector, "ok": r.ok,
                          "reason": r.reason} for r in self.dkim]}


@dataclass
class GateResult:
    accepted: bool
    reason: str | None  # set when not accepted
    sender: str | None  # the identity that passed (or was checked)
    auth: AuthVerdict | None


def identities(parsed: ParsedEmail, envelope_from: str | None) -> list[str]:
    """Who sent this: the header From (manual forwards), then the envelope sender
    (auto-forward rules keep the original From but send from the client)."""
    found = []
    for candidate in (parsed.sender.address, envelope_from or ""):
        candidate = candidate.strip()
        if candidate and "@" in candidate and candidate.lower() not in [c.lower() for c in found]:
            found.append(candidate)
    return found


async def check_sender(
    raw: bytes, parsed: ParsedEmail, *, envelope_from: str | None,
    allowed_senders: list[str], require_auth: bool, resolver: TxtResolver,
) -> GateResult:
    """Allowlist first (cheap), then a DKIM signature aligned with that sender."""
    if not [s for s in allowed_senders if s.strip()]:
        return GateResult(False, "allowlist_empty", None, None)
    candidates = [c for c in identities(parsed, envelope_from)
                  if sender_allowed(c, allowed_senders)]
    if not candidates:
        return GateResult(False, "sender_not_allowed",
                          parsed.sender.address or envelope_from, None)
    if not require_auth:
        return GateResult(True, None, candidates[0], AuthVerdict(False, detail="not required"))
    results = await verify_message(raw, resolver)
    for candidate in candidates:
        for result in results:
            if result.ok and aligned(result.domain, _domain(candidate)):
                return GateResult(True, None, candidate, AuthVerdict(
                    True, "dkim", result.domain, results,
                    f"DKIM pass for {result.domain}, aligned with {candidate}"))
    if not results:
        detail = "no DKIM signature"
    elif any(r.ok for r in results):
        detail = ("DKIM pass only for " + ", ".join(sorted({r.domain for r in results if r.ok}))
                  + "; not aligned with " + ", ".join(candidates))
    else:
        detail = "; ".join(f"{r.domain or '?'}: {r.reason}" for r in results)
    return GateResult(False, "auth_failed", candidates[0],
                      AuthVerdict(False, None, None, results, detail))


# ── acting user ────────────────────────────────────────────────────────────


async def resolve_act_as(users: Any, value: str | None) -> str | None:
    """The handler user id in `email.act_as`, if it names an enabled user.

    Same rule as chat's X-Handler-User: the user must exist and be enabled.
    Accepts "17" or "user:17". On the Crontal instance, handler user ids are
    Crontal user ids, so crontal_api_* tools mint that user's JWT.
    """
    user_id = (value or "").strip().removeprefix("user:")
    if not user_id:
        return None
    get_user = getattr(users, "get_user", None)
    if get_user is None:
        return user_id
    user = await get_user(user_id)
    if user is None or getattr(user, "disabled", False):
        return None
    return user.id


# ── files ──────────────────────────────────────────────────────────────────

WriteFile = Callable[[str, bytes], Awaitable[None]]  # workspace-relative path, data


def _extract_text(path: Path) -> str | None:
    if path.suffix.lower() not in _EXTRACTABLE:
        return None
    try:
        from markitdown import MarkItDown

        return MarkItDown().convert(str(path)).text_content
    except Exception:  # noqa: BLE001 - extraction is best effort
        return None


async def save_files(parsed: ParsedEmail, raw: bytes, write: WriteFile, *,
                     scratch: Path) -> dict[str, Any]:
    """Write the .eml, body and attachments under inbox/<date>-<slug>/.

    Documents also get a text rendering under extracted/ (markitdown, the same
    converter crontal_api uses for downloads), within a time budget.
    """
    folder = f"inbox/{folder_name(parsed)}"
    await write(f"{folder}/message.eml", raw)
    await write(f"{folder}/body.txt", parsed.body_text.encode())
    files: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + EXTRACT_BUDGET_SECONDS
    scratch.mkdir(parents=True, exist_ok=True)
    for attachment in parsed.attachments:
        path = f"{folder}/attachments/{attachment.filename}"
        await write(path, attachment.data)
        entry: dict[str, Any] = {
            "path": path, "filename": attachment.filename,
            "content_type": attachment.content_type or (
                mimetypes.guess_type(attachment.filename)[0] or "application/octet-stream"),
            "size": attachment.size, "from_forwarded": attachment.origin or None,
        }
        remaining = deadline - loop.time()
        if (attachment.size <= MAX_EXTRACT_BYTES and remaining > 1
                and Path(attachment.filename).suffix.lower() in _EXTRACTABLE):
            local = scratch / attachment.filename
            local.write_bytes(attachment.data)
            try:
                text = await asyncio.wait_for(asyncio.to_thread(_extract_text, local),
                                              timeout=min(EXTRACT_TIMEOUT_SECONDS, remaining))
            except TimeoutError:
                text = None
                entry["extract_error"] = "timed out"
            if text and text.strip():
                text_path = f"{folder}/extracted/{attachment.filename}.md"
                await write(text_path, text.encode())
                entry["text_path"] = text_path
        files.append(entry)
    return {"dir": folder, "eml": f"{folder}/message.eml", "body": f"{folder}/body.txt",
            "attachments": files, "skipped": list(parsed.skipped)}


# ── event payload and turn ─────────────────────────────────────────────────


def _addr_dict(address: Address) -> dict:
    return address.as_dict()


def build_payload(parsed: ParsedEmail, *, envelope_from: str | None, envelope_to: str | None,
                  auth: AuthVerdict | None, header_auth: dict | None = None,
                  workspace: dict | None = None, ignored_reason: str | None = None,
                  forwarded_copy: str | None = None) -> dict[str, Any]:
    body = parsed.body_text
    payload: dict[str, Any] = {
        "message_id": parsed.message_id,
        "root_message_id": parsed.root_message_id,
        "subject": parsed.subject,
        "date": parsed.date,
        "date_iso": parsed.date_iso,
        "forwarder": _addr_dict(parsed.sender),
        "to": [a.as_dict() for a in parsed.to],
        "cc": [a.as_dict() for a in parsed.cc],
        "envelope": {"from": envelope_from, "to": envelope_to},
        "original": parsed.original.as_dict() if parsed.original else None,
        "originals": [o.as_dict() for o in parsed.originals],
        "note": parsed.note[:MAX_BODY_CHARS],
        "body_text": body[:MAX_BODY_CHARS],
        "body_truncated": len(body) > MAX_BODY_CHARS,
        "auth": auth.as_dict() if auth else None,
        "header_auth": header_auth or None,  # informational only, never trusted
        "forwarded_copy": forwarded_copy,
        "attachment_names": [a.filename for a in parsed.attachments],
    }
    if workspace is not None:
        payload["workspace"] = workspace
    if ignored_reason is not None:
        payload["ignored_reason"] = ignored_reason
        payload.pop("body_text")
        payload.pop("note")
        payload["body_preview"] = body[:500]
    return payload


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n} B"


def _who(value: dict | None) -> str:
    if not value:
        return "(unknown)"
    name, address = value.get("name") or "", value.get("address") or ""
    return f"{name} <{address}>" if name and address else address or name or "(unknown)"


def render_email_event(payload: dict[str, Any], source: str = "email") -> str:
    """The turn text for one email.received event (design doc §3.1 style)."""
    lines = [f'<event type="email.received" source="{source}">']
    envelope = payload.get("envelope") or {}
    forwarder = _who(payload.get("forwarder"))
    if envelope.get("from") and envelope["from"] != (payload.get("forwarder") or {}).get("address"):
        forwarder += f" (envelope sender {envelope['from']})"
    lines.append(f"Forwarded by: {forwarder}")
    lines.append(f"Received at: {envelope.get('to') or 'contact address'}")
    lines.append(f"Date: {payload.get('date') or '(none)'}")
    lines.append(f"Subject: {payload.get('subject') or '(none)'}")
    originals = payload.get("originals") or []
    for i, original in enumerate(originals, start=1):
        label = "Original message" if len(originals) == 1 else f"Original message {i}"
        how = "inline in the body" if original.get("source") == "inline" else (
            f"attached as {original.get('eml')}" if original.get("eml") else "attached")
        lines.append(f"{label} ({how}):")
        lines.append(f"  From: {_who(original.get('from'))}")
        lines.append(f"  Date: {original.get('date') or '(unknown)'}")
        lines.append(f"  Subject: {original.get('subject') or '(none)'}")
    if not originals:
        lines.append("Original message: none detected (treat the forwarder as the sender).")
    workspace = payload.get("workspace") or {}
    if workspace:
        lines.append("Files in your workspace:")
        lines.append(f"- {workspace.get('eml')} (raw email)")
        lines.append(f"- {workspace.get('body')} (full body text)")
        for item in workspace.get("attachments") or []:
            extra = f"; text: {item['text_path']}" if item.get("text_path") else ""
            origin = (f"; from {item['from_forwarded']}" if item.get("from_forwarded") else "")
            lines.append(f"- {item['path']} ({item.get('content_type')}, "
                         f"{_size(int(item.get('size') or 0))}{origin}{extra})")
        for skipped in workspace.get("skipped") or []:
            lines.append(f"- skipped: {skipped}")
    auth = payload.get("auth") or {}
    if auth.get("passed"):
        lines.append(f"Sender check: {auth.get('detail')}")
    lines.append("")
    lines.append("Body (plain text):")
    lines.append(payload.get("body_text") or "(empty)")
    if payload.get("body_truncated"):
        lines.append(f"[body truncated; full text in {workspace.get('body', 'body.txt')}]")
    lines.append("</event>")
    # Untrusted text must not close the tag early and pose as a system event.
    inner = "\n".join(lines[1:-1]).replace("</event", "&lt;/event")
    return "\n".join((lines[0], inner, lines[-1]))
