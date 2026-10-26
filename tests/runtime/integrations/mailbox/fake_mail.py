"""Fake mail servers for the mailbox integration tests.

- `FakeMailServer`: the mailbox data (folders, UIDs, UIDVALIDITY, accounts).
- `FakeTransport`: the in-memory `MailTransport` (the client-wrapper boundary)
  used by service, tool and API tests.
- `ImapSocketServer` / `SmtpSocketServer`: tiny real servers over TCP with
  implicit TLS or STARTTLS, so `StdlibTransport` (imaplib/smtplib) is tested
  against the wire protocol too.
"""
from __future__ import annotations

import base64
import datetime as dt
import email.utils
import ipaddress
import re
import socket
import socketserver
import ssl
import threading
from dataclasses import dataclass, field
from pathlib import Path

from handler.runtime.integrations.mailbox.transport import (
    FolderInfo,
    FolderStatus,
    MailAuthError,
    MailError,
    MessageMeta,
    ServerParams,
    decode_folder,
    encode_folder,
)

UTC = dt.timezone.utc


@dataclass
class FakeFolder:
    uidvalidity: int = 1000
    uidnext: int = 1
    messages: dict[int, tuple[bytes, dt.datetime, str]] = field(default_factory=dict)
    flags: tuple[str, ...] = ()


class FakeMailServer:
    def __init__(self, username="sales@example.cn", password="code-1234") -> None:
        self.username, self.password = username, password
        self.folders: dict[str, FakeFolder] = {
            "INBOX": FakeFolder(), "Sent": FakeFolder(flags=("\\Sent",)),
            "草稿箱": FakeFolder(flags=("\\Drafts",))}
        self.sent: list[tuple[str, list[str], bytes]] = []
        self.logins = 0
        self.lock = threading.Lock()

    def deliver(self, raw: bytes, folder="INBOX", when: dt.datetime | None = None) -> int:
        with self.lock:
            target = self.folders[folder]
            uid = target.uidnext
            target.uidnext += 1
            target.messages[uid] = (raw, when or dt.datetime.now(UTC), "")
            return uid

    def renumber(self, folder="INBOX") -> None:
        """Simulate a server that rebuilt the folder: new UIDVALIDITY and UIDs."""
        with self.lock:
            old = self.folders[folder]
            new = FakeFolder(uidvalidity=old.uidvalidity + 1, uidnext=1, flags=old.flags)
            for _, value in sorted(old.messages.items()):
                new.messages[new.uidnext + 100] = value
                new.uidnext += 1
            new.uidnext += 100
            self.folders[folder] = new

    # protocol helpers shared by the in-memory session and the socket server

    def search(self, folder: str, criteria: str) -> list[int]:
        target = self.folders[folder]
        uids = sorted(target.messages)
        words = criteria.split()
        result = uids
        i = 0
        while i < len(words):
            word = words[i].upper()
            if word == "ALL":
                i += 1
            elif word == "UID":
                low, _, high = words[i + 1].partition(":")
                lo = int(low)
                hi = None if high == "*" else int(high or low)
                picked = [u for u in result if u >= lo and (hi is None or u <= hi)]
                if high == "*" and not picked and uids:
                    picked = [uids[-1]]  # RFC 3501: "n:*" always includes the top UID
                result = picked
                i += 2
            elif word == "SINCE":
                day = dt.datetime.strptime(words[i + 1], "%d-%b-%Y").date()
                result = [u for u in result if target.messages[u][1].date() >= day]
                i += 2
            else:
                raise MailError(f"unsupported search {criteria}")
        return result


class FakeImapSession:
    def __init__(self, server: FakeMailServer) -> None:
        self.server = server
        self.capabilities = frozenset({"IMAP4REV1", "UIDPLUS", "AUTH=PLAIN"})
        self.selected: str | None = None
        self.peeked: list[int] = []

    def list_folders(self):
        return [FolderInfo(name, f.flags, "/") for name, f in self.server.folders.items()]

    def select(self, folder, *, readonly=True):
        if folder not in self.server.folders:
            raise MailError(f"IMAP SELECT {folder} failed: no such folder")
        self.selected = folder
        f = self.server.folders[folder]
        return FolderStatus(f.uidvalidity, f.uidnext, len(f.messages))

    def uid_search(self, criteria):
        return self.server.search(self.selected, criteria)

    def fetch_meta(self, uids):
        f = self.server.folders[self.selected]
        return [MessageMeta(u, len(f.messages[u][0]), f.messages[u][1])
                for u in sorted(uids) if u in f.messages]

    def fetch_message(self, uid):
        self.peeked.append(uid)
        item = self.server.folders[self.selected].messages.get(uid)
        return item[0] if item else None

    def fetch_header(self, uid):
        raw = self.fetch_message(uid)
        return raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if raw else None

    def append(self, folder, data, *, flags="", when=None):
        if folder not in self.server.folders:
            raise MailError(f"IMAP APPEND {folder} failed")
        self.server.deliver(data, folder)

    def logout(self):
        pass


class FakeSmtpSession:
    auth_mechanisms = ("PLAIN", "LOGIN")

    def __init__(self, server):
        self.server = server

    def send(self, sender, recipients, data):
        self.server.sent.append((sender, list(recipients), data))
        return {}

    def close(self):
        pass


class FakeTransport:
    def __init__(self, server: FakeMailServer) -> None:
        self.server = server
        self.imap_opens = 0
        self.fail_with: Exception | None = None

    def _auth(self, params: ServerParams):
        if self.fail_with is not None:
            raise self.fail_with
        if params.username != self.server.username or params.password != self.server.password:
            raise MailAuthError(f"IMAP {params.host}:{params.port}: login failed "
                                "([AUTHENTICATIONFAILED] invalid credentials)")
        self.server.logins += 1

    def open_imap(self, params):
        self._auth(params)
        self.imap_opens += 1
        return FakeImapSession(self.server)

    def open_smtp(self, params):
        self._auth(params)
        return FakeSmtpSession(self.server)


# ── messages ───────────────────────────────────────────────────────────────


def make_mail(*, subject="Quote request", sender="Buyer <buyer@client.cn>",
              to="sales@example.cn", body="Please quote 304 pipe.", message_id=None,
              in_reply_to=None, references=None, date=None, charset="utf-8",
              attachments=(), html=None) -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(date or dt.datetime.now(UTC))
    msg["Message-ID"] = message_id or email.utils.make_msgid(domain="client.cn")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body, charset=charset)
    if html:
        msg.add_alternative(html, subtype="html")
    for name, data, mime in attachments:
        main, _, sub = mime.partition("/")
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return msg.as_bytes()


# ── TLS material ───────────────────────────────────────────────────────────


def self_signed_cert(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _LineIO:
    def __init__(self, sock):
        self.sock = sock
        self.file = sock.makefile("rb")

    def rewrap(self, sock):
        self.sock = sock
        self.file = sock.makefile("rb")

    def readline(self) -> bytes:
        return self.file.readline()

    def read(self, n: int) -> bytes:
        return self.file.read(n)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)


class _Base:
    def __init__(self, mail: FakeMailServer, cert: tuple[Path, Path], *, implicit_tls: bool,
                 starttls: bool = True) -> None:
        self.mail = mail
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(*map(str, cert))
        self.implicit_tls = implicit_tls
        self.starttls = starttls
        self.commands: list[str] = []
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock = self.request
                if outer.implicit_tls:
                    try:
                        sock = outer.context.wrap_socket(sock, server_side=True)
                    except (ssl.SSLError, OSError):
                        return
                try:
                    outer.session(_LineIO(sock))
                except (OSError, ssl.SSLError, ValueError):
                    pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _quoted(text: str) -> str:
    text = text.strip()
    if text.startswith('"') and text.endswith('"'):
        return re.sub(r'\\(.)', r"\1", text[1:-1])
    return text


class ImapSocketServer(_Base):
    def caps(self, tls: bool) -> str:
        words = ["IMAP4rev1", "UIDPLUS"]
        if tls:
            words.append("AUTH=PLAIN")
        elif self.starttls:
            words += ["STARTTLS", "LOGINDISABLED"]
        return " ".join(words)

    def session(self, io: _LineIO) -> None:
        tls = self.implicit_tls
        authed = False
        selected = None
        io.send(f"* OK [CAPABILITY {self.caps(tls)}] fake ready\r\n".encode())
        while True:
            line = io.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            tag, _, rest = text.partition(" ")
            command, _, args = rest.partition(" ")
            command = command.upper()
            self.commands.append(command if command not in ("LOGIN", "AUTHENTICATE")
                                 else command + " ***")
            if command == "CAPABILITY":
                io.send(f"* CAPABILITY {self.caps(tls)}\r\n{tag} OK done\r\n".encode())
            elif command == "STARTTLS" and not tls and self.starttls:
                io.send(f"{tag} OK begin TLS\r\n".encode())
                sock = self.context.wrap_socket(io.sock, server_side=True)
                io.rewrap(sock)
                tls = True
            elif command == "AUTHENTICATE" and args.upper().startswith("PLAIN"):
                io.send(b"+ \r\n")
                payload = base64.b64decode(io.readline().strip())
                _, user, password = payload.split(b"\0")
                if tls and user.decode() == self.mail.username and \
                        password.decode() == self.mail.password:
                    authed = True
                    self.mail.logins += 1
                    io.send(f"{tag} OK authenticated\r\n".encode())
                else:
                    io.send(f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials\r\n".encode())
            elif command == "LOGIN":
                io.send(f"{tag} NO LOGIN disabled\r\n".encode())
            elif command == "LOGOUT":
                io.send(f"* BYE\r\n{tag} OK bye\r\n".encode())
                return
            elif not authed:
                io.send(f"{tag} NO not authenticated\r\n".encode())
            elif command == "LIST":
                out = ""
                for name, folder in self.mail.folders.items():
                    flags = " ".join(("\\HasNoChildren",) + folder.flags)
                    out += f'* LIST ({flags}) "/" "{encode_folder(name)}"\r\n'
                io.send((out + f"{tag} OK listed\r\n").encode())
            elif command in ("SELECT", "EXAMINE"):
                selected = decode_folder(_quoted(args))
                folder = self.mail.folders.get(selected)
                if folder is None:
                    io.send(f"{tag} NO no such folder\r\n".encode())
                    continue
                io.send((f"* {len(folder.messages)} EXISTS\r\n* 0 RECENT\r\n"
                         f"* OK [UIDVALIDITY {folder.uidvalidity}] ok\r\n"
                         f"* OK [UIDNEXT {folder.uidnext}] ok\r\n"
                         f"{tag} OK [{'READ-ONLY' if command == 'EXAMINE' else 'READ-WRITE'}] "
                         "selected\r\n").encode())
            elif command == "UID":
                sub, _, sub_args = args.partition(" ")
                if sub.upper() == "SEARCH":
                    uids = self.mail.search(selected, sub_args)
                    io.send(f"* SEARCH {' '.join(map(str, uids))}\r\n{tag} OK searched\r\n".encode())
                elif sub.upper() == "FETCH":
                    uid_spec, _, items = sub_args.partition(" ")
                    folder = self.mail.folders[selected]
                    wanted = set()
                    for part in uid_spec.split(","):
                        low, _, high = part.partition(":")
                        wanted |= {u for u in folder.messages
                                   if int(low) <= u <= int(high or low)}
                    out = b""
                    for seq, uid in enumerate(sorted(wanted), start=1):
                        raw, when, _ = folder.messages[uid]
                        if "BODY.PEEK[HEADER]" in items:
                            data = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                            out += (f"* {seq} FETCH (UID {uid} BODY[HEADER] {{{len(data)}}}\r\n"
                                    ).encode() + data + b")\r\n"
                        elif "BODY.PEEK[]" in items:
                            out += f"* {seq} FETCH (UID {uid} BODY[] {{{len(raw)}}}\r\n".encode() \
                                + raw + b")\r\n"
                        else:
                            stamp = when.strftime("%d-%b-%Y %H:%M:%S +0000")
                            out += (f'* {seq} FETCH (UID {uid} RFC822.SIZE {len(raw)} '
                                    f'INTERNALDATE "{stamp}")\r\n').encode()
                    io.send(out + f"{tag} OK fetched\r\n".encode())
                else:
                    io.send(f"{tag} BAD unsupported\r\n".encode())
            elif command == "APPEND":
                match = re.search(r"\{(\d+)\}$", args)
                folder = decode_folder(_quoted(args.split(" (")[0]))
                io.send(b"+ ready\r\n")
                data = io.read(int(match.group(1)))
                io.readline()
                self.mail.deliver(data, folder)
                io.send(f"{tag} OK [APPENDUID 1 1] appended\r\n".encode())
            elif command == "STATUS":
                io.send(f"{tag} NO unsupported\r\n".encode())
            else:
                io.send(f"{tag} BAD unknown command\r\n".encode())


class SmtpSocketServer(_Base):
    def session(self, io: _LineIO) -> None:
        tls = self.implicit_tls
        authed = False
        sender, recipients = None, []
        io.send(b"220 fake ESMTP\r\n")
        while True:
            line = io.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            verb = text.split(" ", 1)[0].upper()
            self.commands.append(verb)
            if verb in ("EHLO", "HELO"):
                lines = ["250-fake"]
                if not tls and self.starttls:
                    lines.append("250-STARTTLS")
                if tls:
                    lines.append("250-AUTH PLAIN LOGIN")
                lines.append("250 8BITMIME")
                io.send(("\r\n".join(lines) + "\r\n").encode())
            elif verb == "STARTTLS" and not tls:
                io.send(b"220 go ahead\r\n")
                io.rewrap(self.context.wrap_socket(io.sock, server_side=True))
                tls = True
            elif verb == "AUTH":
                parts = text.split(" ")
                payload = base64.b64decode(parts[2]) if len(parts) > 2 else b""
                pieces = payload.split(b"\0")
                if tls and len(pieces) == 3 and pieces[1].decode() == self.mail.username \
                        and pieces[2].decode() == self.mail.password:
                    authed = True
                    io.send(b"235 ok\r\n")
                else:
                    io.send(b"535 5.7.8 authentication failed\r\n")
            elif verb == "MAIL" and authed:
                sender = re.search(r"<([^>]*)>", text).group(1)
                recipients = []
                io.send(b"250 ok\r\n")
            elif verb == "RCPT" and authed:
                recipients.append(re.search(r"<([^>]*)>", text).group(1))
                io.send(b"250 ok\r\n")
            elif verb == "DATA" and authed:
                io.send(b"354 go\r\n")
                chunks = []
                while True:
                    chunk = io.readline()
                    if chunk in (b".\r\n", b""):
                        break
                    chunks.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                self.mail.sent.append((sender, recipients, b"".join(chunks)))
                io.send(b"250 queued\r\n")
            elif verb == "QUIT":
                io.send(b"221 bye\r\n")
                return
            elif verb in ("RSET", "NOOP"):
                io.send(b"250 ok\r\n")
            else:
                io.send(b"530 authentication required\r\n")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
