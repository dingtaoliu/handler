"""StdlibTransport (imaplib/smtplib) against small real servers over TLS."""
import datetime as dt

import pytest

from handler.runtime.integrations.mailbox.transport import (
    MailAuthError,
    MailError,
    ServerParams,
    StdlibTransport,
    decode_folder,
    encode_folder,
    parse_list_line,
    uid_set,
)

from .fake_mail import FakeMailServer, ImapSocketServer, SmtpSocketServer, make_mail, self_signed_cert

UTC = dt.timezone.utc


@pytest.fixture
def cert(tmp_path):
    return self_signed_cert(tmp_path)


def params(port, security, *, password="code-1234", allow_self_signed=True):
    return ServerParams("127.0.0.1", port, security, "sales@example.cn", password,
                        allow_self_signed=allow_self_signed, timeout=5)


def test_modified_utf7_folder_names():
    assert encode_folder("草稿箱") == "&g0l6P3ux-"
    assert decode_folder("&g0l6P3ux-") == "草稿箱"
    assert decode_folder(encode_folder("已发送 & Sent")) == "已发送 & Sent"
    assert encode_folder("A&B") == "A&-B"
    info = parse_list_line(rb'(\HasNoChildren \Sent) "/" "&XfJT0ZAB-"')
    assert info.name == "已发送" and "\\Sent" in info.flags and info.delimiter == "/"
    assert uid_set([1, 2, 3, 7, 9, 10]) == "1:3,7,9:10"


@pytest.mark.parametrize("security", ["tls", "starttls"])
def test_imap_sync_primitives_over_the_wire(cert, security):
    mail = FakeMailServer()
    old = dt.datetime(2026, 9, 1, tzinfo=UTC)
    mail.deliver(make_mail(subject="old"), when=old)
    uid = mail.deliver(make_mail(subject="报价 新"))
    with ImapSocketServer(mail, cert, implicit_tls=security == "tls") as server:
        session = StdlibTransport().open_imap(params(server.port, security))
        try:
            assert "UIDPLUS" in session.capabilities
            names = {f.name: f.flags for f in session.list_folders()}
            assert "草稿箱" in names and "\\Drafts" in names["草稿箱"]
            status = session.select("INBOX")
            assert (status.uidvalidity, status.uidnext, status.exists) == (1000, 3, 2)
            assert session.uid_search("UID 1:*") == [1, 2]
            assert session.uid_search("UID 1:2 SINCE 01-Oct-2026") == [2]
            meta = session.fetch_meta([1, 2])
            assert [m.uid for m in meta] == [1, 2] and meta[0].internaldate == old
            raw = session.fetch_message(uid)
            assert b"Subject:" in raw and raw == mail.folders["INBOX"].messages[uid][0]
            assert session.fetch_header(uid).endswith(b"\r\n\r\n")
            session.select("草稿箱", readonly=False)
            session.append("草稿箱", make_mail(subject="draft"), flags="(\\Draft)")
            assert len(mail.folders["草稿箱"].messages) == 1
        finally:
            session.logout()
        # Credentials only ever went over TLS.
        if security == "starttls":
            assert server.commands.index("STARTTLS") < server.commands.index("AUTHENTICATE ***")


def test_imap_refuses_plaintext_when_starttls_is_missing(cert):
    with ImapSocketServer(FakeMailServer(), cert, implicit_tls=False, starttls=False) as server:
        with pytest.raises(MailError, match="does not offer STARTTLS"):
            StdlibTransport().open_imap(params(server.port, "starttls"))
        assert not any(c.startswith(("LOGIN", "AUTHENTICATE")) for c in server.commands)


def test_certificates_are_verified_by_default(cert):
    with ImapSocketServer(FakeMailServer(), cert, implicit_tls=True) as server:
        with pytest.raises(MailError, match="could not be verified.*self-signed"):
            StdlibTransport().open_imap(params(server.port, "tls", allow_self_signed=False))


def test_imap_login_failure_is_clear_and_secret_free(cert):
    with ImapSocketServer(FakeMailServer(), cert, implicit_tls=True) as server:
        with pytest.raises(MailAuthError) as caught:
            StdlibTransport().open_imap(params(server.port, "tls", password="wrong-secret-9"))
    message = str(caught.value)
    assert "login failed" in message and "authorization code" in message
    assert "wrong-secret-9" not in message


@pytest.mark.parametrize("security", ["tls", "starttls"])
def test_smtp_login_and_send(cert, security):
    mail = FakeMailServer()
    with SmtpSocketServer(mail, cert, implicit_tls=security == "tls") as server:
        client = StdlibTransport().open_smtp(params(server.port, security))
        assert "PLAIN" in client.auth_mechanisms
        client.send("sales@example.cn", ["buyer@client.cn"], make_mail(subject="hi"))
        client.close()
    assert mail.sent[0][1] == ["buyer@client.cn"]


def test_smtp_bad_password(cert):
    with SmtpSocketServer(FakeMailServer(), cert, implicit_tls=True) as server:
        with pytest.raises(MailAuthError) as caught:
            StdlibTransport().open_smtp(params(server.port, "tls", password="nope-123"))
    assert "nope-123" not in str(caught.value) and "535" in str(caught.value)


def test_smtp_refuses_plaintext(cert):
    with SmtpSocketServer(FakeMailServer(), cert, implicit_tls=False, starttls=False) as server:
        with pytest.raises(MailError, match="does not offer STARTTLS"):
            StdlibTransport().open_smtp(params(server.port, "starttls"))
        assert "AUTH" not in server.commands
