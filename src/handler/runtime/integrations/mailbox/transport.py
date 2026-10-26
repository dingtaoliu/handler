"""IMAP and SMTP clients behind a small interface (the test boundary).

The real implementation uses the stdlib (`imaplib`, `smtplib`). Both are
blocking, so callers run every method in a worker thread
(`asyncio.to_thread`); a session is only ever used by one thread at a time.

Security rules enforced here, whatever the caller asks for:
- TLS always: `tls` (implicit, e.g. 993/465) or `starttls` (143/587). A
  server that does not offer STARTTLS is refused before any credentials are
  sent; there is no plaintext mode.
- Certificates are verified (system trust store, hostname checked) unless the
  mailbox explicitly allows self-signed certificates.
- The password never appears in an error message (`redact`).
- Mail is read with BODY.PEEK, and folders are opened read-only for polling,
  so Handler never marks messages as read or changes flags.
"""

from __future__ import annotations

import base64
import imaplib
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

CONNECT_TIMEOUT_SECONDS = 30.0
SECURITY_MODES = ("tls", "starttls")
# Address literal for EHLO: the dyno's hostname is meaningless to the server
# and getfqdn() can block on DNS.
EHLO_NAME = "[127.0.0.1]"


class MailError(Exception):
    """A mail server problem; the message is safe to show (no secrets)."""


class MailAuthError(MailError):
    """The server rejected the username or password."""


@dataclass(frozen=True)
class ServerParams:
    host: str
    port: int
    security: str  # "tls" | "starttls"
    username: str
    password: str = field(repr=False)
    allow_self_signed: bool = False
    timeout: float = CONNECT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class FolderStatus:
    uidvalidity: int
    uidnext: int | None
    exists: int


@dataclass(frozen=True)
class FolderInfo:
    name: str  # decoded (Unicode) name
    flags: tuple[str, ...] = ()
    delimiter: str | None = None


@dataclass(frozen=True)
class MessageMeta:
    uid: int
    size: int
    internaldate: datetime | None


class ImapSession(Protocol):
    capabilities: frozenset[str]

    def list_folders(self) -> list[FolderInfo]: ...
    def select(self, folder: str, *, readonly: bool = True) -> FolderStatus: ...
    def uid_search(self, criteria: str) -> list[int]: ...
    def fetch_meta(self, uids: list[int]) -> list[MessageMeta]: ...
    def fetch_message(self, uid: int) -> bytes | None: ...
    def fetch_header(self, uid: int) -> bytes | None: ...
    def append(self, folder: str, data: bytes, *, flags: str = "",
               when: datetime | None = None) -> None: ...
    def logout(self) -> None: ...


class SmtpSession(Protocol):
    auth_mechanisms: tuple[str, ...]

    def send(self, sender: str, recipients: list[str], data: bytes) -> dict: ...
    def close(self) -> None: ...


class MailTransport(Protocol):
    def open_imap(self, params: ServerParams) -> ImapSession: ...
    def open_smtp(self, params: ServerParams) -> SmtpSession: ...


# ── helpers ────────────────────────────────────────────────────────────────


def redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def tls_context(allow_self_signed: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if allow_self_signed:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def describe_error(exc: BaseException, params: ServerParams, protocol: str) -> MailError:
    """A clear, secret-free MailError for a connection or login failure."""
    where = f"{protocol} {params.host}:{params.port}"
    if isinstance(exc, MailError):
        return type(exc)(redact(str(exc), params.password))
    if isinstance(exc, ssl.SSLCertVerificationError):
        return MailError(
            f"{where}: the server's TLS certificate could not be verified "
            f"({exc.verify_message or exc.reason}). If this server uses a self-signed "
            "certificate, enable 'Allow self-signed certificate' for this mailbox.")
    if isinstance(exc, ssl.SSLError):
        hint = (" Check the security setting: port 993/465 use TLS, 143/587 use STARTTLS."
                if "WRONG_VERSION_NUMBER" in str(exc) or "wrong version" in str(exc).lower()
                else "")
        return MailError(f"{where}: TLS handshake failed ({exc.reason or exc}).{hint}")
    if isinstance(exc, socket.gaierror):
        return MailError(f"{where}: host not found")
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return MailError(f"{where}: timed out")
    if isinstance(exc, ConnectionRefusedError):
        return MailError(f"{where}: connection refused")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        detail = _server_text(exc.smtp_error)
        return MailAuthError(redact(f"{where}: login failed ({exc.smtp_code} {detail}). "
                                    + AUTH_HINT, params.password))
    if isinstance(exc, (smtplib.SMTPException, imaplib.IMAP4.error, OSError)):
        return MailError(redact(f"{where}: {type(exc).__name__}: {exc}", params.password))
    return MailError(redact(f"{where}: {type(exc).__name__}: {exc}", params.password))


AUTH_HINT = ("Check the username and password. Many providers (QQ, 163, Coremail-based "
             "hosts such as chengmail.cn) require a client authorization code instead "
             "of the web password, and IMAP/SMTP must be enabled in the webmail settings.")


def _server_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


# ── IMAP modified UTF-7 (RFC 3501 §5.1.3) ─────────────────────────────────


def encode_folder(name: str) -> str:
    out, buffer = [], []

    def flush():
        if buffer:
            raw = "".join(buffer).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
            buffer.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buffer.append(ch)
    flush()
    return "".join(out)


def decode_folder(name: str) -> str:
    def repl(match: re.Match) -> str:
        body = match.group(1)
        if not body:
            return "&"
        data = body.replace(",", "/")
        data += "=" * (-len(data) % 4)
        try:
            return base64.b64decode(data).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return match.group(0)

    return re.sub(r"&([A-Za-z0-9+,]*)-", repl, name)


def quote_folder(name: str) -> str:
    encoded = encode_folder(name)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')


def parse_list_line(line: bytes | tuple) -> FolderInfo | None:
    if isinstance(line, tuple):  # name sent as a literal
        head, literal = line[0], line[1]
        match = _LIST_LINE.match(head.rstrip() + b' ""')
        if not match:
            return None
        name = literal.decode("utf-8", errors="replace")
    else:
        match = _LIST_LINE.match(line or b"")
        if not match:
            return None
        name = match.group("name").decode("utf-8", errors="replace").strip()
        if name.startswith('"') and name.endswith('"'):
            name = re.sub(r'\\(.)', r"\1", name[1:-1])
    flags = tuple(f.decode() for f in match.group("flags").split())
    delim = match.group("delim").decode()
    delimiter = None if delim == "NIL" else re.sub(r'\\(.)', r"\1", delim[1:-1])
    return FolderInfo(decode_folder(name), flags, delimiter)


def parse_fetch_meta(data: list) -> list[MessageMeta]:
    out = []
    for item in data or []:
        line = item[0] if isinstance(item, tuple) else item
        if not isinstance(line, bytes):
            continue
        uid = re.search(rb"UID (\d+)", line)
        size = re.search(rb"RFC822\.SIZE (\d+)", line)
        if not uid:
            continue
        when = None
        stamp = imaplib.Internaldate2tuple(line) if b"INTERNALDATE" in line else None
        if stamp is not None:
            import time

            when = datetime.fromtimestamp(time.mktime(stamp), tz=timezone.utc)
        out.append(MessageMeta(int(uid.group(1)), int(size.group(1)) if size else 0, when))
    return out


def fetch_literal(data: list) -> bytes | None:
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def uid_set(uids: list[int]) -> str:
    """Compact IMAP sequence set ("1:3,7,9:10")."""
    ordered = sorted(set(uids))
    parts, start, prev = [], None, None
    for uid in ordered:
        if start is None:
            start = prev = uid
        elif uid == prev + 1:
            prev = uid
        else:
            parts.append(f"{start}:{prev}" if start != prev else str(start))
            start = prev = uid
    if start is not None:
        parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)


# ── stdlib implementation ──────────────────────────────────────────────────


class ImaplibSession:
    def __init__(self, params: ServerParams) -> None:
        if params.security not in SECURITY_MODES:
            raise MailError(f"unsupported security mode {params.security!r}")
        context = tls_context(params.allow_self_signed)
        self._params = params
        if params.security == "tls":
            self._imap = imaplib.IMAP4_SSL(params.host, params.port, ssl_context=context,
                                           timeout=params.timeout)
        else:
            self._imap = imaplib.IMAP4(params.host, params.port, timeout=params.timeout)
            if "STARTTLS" not in self._imap.capabilities:
                self._close()
                raise MailError(f"IMAP {params.host}:{params.port} does not offer STARTTLS; "
                                "refusing to send the password unencrypted. Use port 993 "
                                "with TLS.")
            self._imap.starttls(ssl_context=context)
        try:
            caps = set(self._imap.capabilities)
            if "AUTH=PLAIN" in caps:
                creds = f"\0{params.username}\0{params.password}".encode()
                self._imap.authenticate("PLAIN", lambda _challenge: creds)
            else:
                self._imap.login(params.username, params.password)
        except imaplib.IMAP4.error as exc:
            self._close()
            raise MailAuthError(redact(
                f"IMAP {params.host}:{params.port}: login failed ({exc}). " + AUTH_HINT,
                params.password)) from None
        typ, data = self._imap.capability()
        words = data[0].decode(errors="replace").split() if typ == "OK" and data else []
        self.capabilities = frozenset(w.upper() for w in words) or frozenset(
            c.upper() for c in self._imap.capabilities)

    def _close(self) -> None:
        try:
            self._imap.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def _check(self, typ: str, data: list, what: str) -> list:
        if typ != "OK":
            detail = _server_text(data[0] if data else "")
            raise MailError(f"IMAP {what} failed: {detail}"[:500])
        return data

    def list_folders(self) -> list[FolderInfo]:
        typ, data = self._imap.list()
        found = []
        for line in self._check(typ, data, "LIST"):
            info = parse_list_line(line) if line else None
            if info is not None:
                found.append(info)
        return found

    def select(self, folder: str, *, readonly: bool = True) -> FolderStatus:
        typ, data = self._imap.select(quote_folder(folder), readonly=readonly)
        self._check(typ, data, f"SELECT {folder}")
        exists = int((data or [b"0"])[0] or 0)

        def number(name: str) -> int | None:
            _, values = self._imap.response(name)
            for value in values or []:
                if value and re.match(rb"\d+", value):
                    return int(re.match(rb"\d+", value).group(0))
            return None

        uidvalidity, uidnext = number("UIDVALIDITY"), number("UIDNEXT")
        if uidvalidity is None or uidnext is None:
            typ, data = self._imap.status(quote_folder(folder), "(UIDVALIDITY UIDNEXT)")
            if typ == "OK" and data and isinstance(data[0], bytes):
                text = data[0]
                if uidvalidity is None and (m := re.search(rb"UIDVALIDITY (\d+)", text)):
                    uidvalidity = int(m.group(1))
                if uidnext is None and (m := re.search(rb"UIDNEXT (\d+)", text)):
                    uidnext = int(m.group(1))
        if uidvalidity is None:
            raise MailError(f"IMAP server reported no UIDVALIDITY for {folder}")
        return FolderStatus(uidvalidity, uidnext, exists)

    def uid_search(self, criteria: str) -> list[int]:
        typ, data = self._imap.uid("SEARCH", *criteria.split())
        uids: list[int] = []
        for chunk in self._check(typ, data, "SEARCH"):
            if isinstance(chunk, bytes):
                uids.extend(int(x) for x in chunk.split() if x.isdigit())
        return sorted(set(uids))

    def fetch_meta(self, uids: list[int]) -> list[MessageMeta]:
        out: list[MessageMeta] = []
        for start in range(0, len(uids), 200):
            chunk = uids[start:start + 200]
            typ, data = self._imap.uid("FETCH", uid_set(chunk), "(UID RFC822.SIZE INTERNALDATE)")
            out.extend(parse_fetch_meta(self._check(typ, data, "FETCH")))
        return sorted(out, key=lambda m: m.uid)

    def fetch_message(self, uid: int) -> bytes | None:
        typ, data = self._imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        return fetch_literal(self._check(typ, data, "FETCH"))

    def fetch_header(self, uid: int) -> bytes | None:
        typ, data = self._imap.uid("FETCH", str(uid), "(BODY.PEEK[HEADER])")
        return fetch_literal(self._check(typ, data, "FETCH"))

    def append(self, folder: str, data: bytes, *, flags: str = "",
               when: datetime | None = None) -> None:
        stamp = imaplib.Time2Internaldate((when or datetime.now(timezone.utc)).timestamp())
        typ, response = self._imap.append(quote_folder(folder), flags or None, stamp, data)
        self._check(typ, response, f"APPEND {folder}")

    def logout(self) -> None:
        try:
            self._imap.logout()
        except Exception:  # noqa: BLE001 - best effort
            self._close()


class SmtplibSession:
    def __init__(self, params: ServerParams) -> None:
        if params.security not in SECURITY_MODES:
            raise MailError(f"unsupported security mode {params.security!r}")
        context = tls_context(params.allow_self_signed)
        if params.security == "tls":
            self._smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                params.host, params.port, local_hostname=EHLO_NAME, context=context,
                timeout=params.timeout)
            self._smtp.ehlo()
        else:
            self._smtp = smtplib.SMTP(params.host, params.port, local_hostname=EHLO_NAME,
                                      timeout=params.timeout)
            self._smtp.ehlo()
            if not self._smtp.has_extn("starttls"):
                self.close()
                raise MailError(f"SMTP {params.host}:{params.port} does not offer STARTTLS; "
                                "refusing to send the password unencrypted. Use port 465 "
                                "with TLS.")
            self._smtp.starttls(context=context)
            self._smtp.ehlo()
        self.auth_mechanisms = tuple((self._smtp.esmtp_features.get("auth") or "").upper().split())
        self._smtp.login(params.username, params.password)

    def send(self, sender: str, recipients: list[str], data: bytes) -> dict:
        return self._smtp.sendmail(sender, recipients, data)

    def close(self) -> None:
        try:
            self._smtp.quit()
        except Exception:  # noqa: BLE001 - best effort
            try:
                self._smtp.close()
            except Exception:  # noqa: BLE001
                pass


class StdlibTransport:
    """The production transport: imaplib and smtplib over TLS."""

    def open_imap(self, params: ServerParams) -> ImapSession:
        try:
            return ImaplibSession(params)
        except Exception as exc:  # noqa: BLE001 - always a clear, secret-free error
            raise describe_error(exc, params, "IMAP") from None

    def open_smtp(self, params: ServerParams) -> SmtpSession:
        try:
            return SmtplibSession(params)
        except Exception as exc:  # noqa: BLE001
            raise describe_error(exc, params, "SMTP") from None
