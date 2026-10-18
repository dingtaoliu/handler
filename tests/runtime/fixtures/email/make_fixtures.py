"""Regenerate the email fixtures (manual; needs `pip install dkimpy pynacl`).

    python tests/runtime/fixtures/email/make_fixtures.py

Signs each message with dkimpy (an independent DKIM implementation) using
fresh test keys, and writes the matching public key records to keys.json, so
handler's own verifier is checked against a third-party signer.
"""

from __future__ import annotations

import base64
import json
from email import policy
from email.message import EmailMessage
from pathlib import Path

import dkim
import nacl.signing
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

HERE = Path(__file__).resolve().parent
SMTP = policy.SMTP  # CRLF line endings, like mail on the wire


def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.DER,
                                           serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, f"v=DKIM1; k=rsa; p={base64.b64encode(public).decode()}"


def ed_key():
    signing = nacl.signing.SigningKey.generate()
    private = base64.b64encode(bytes(signing))
    public = base64.b64encode(bytes(signing.verify_key)).decode()
    return private, f"v=DKIM1; k=ed25519; p={public}"


def sign(raw: bytes, selector: str, domain: str, key: bytes, *, relaxed=True,
         algorithm=b"rsa-sha256") -> bytes:
    canon = (b"relaxed", b"relaxed") if relaxed else (b"simple", b"simple")
    header = dkim.sign(raw, selector.encode(), domain.encode(), key, canonicalize=canon,
                       signature_algorithm=algorithm,
                       include_headers=[b"from", b"to", b"subject", b"date", b"message-id"])
    return header + raw


def gmail_inline() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Lin Mei <mei.lin@client.example>"
    msg["To"] = "contact@crontal.ai"
    msg["Subject"] = "Fwd: RFQ 316L flat bar - urgent"
    msg["Date"] = "Wed, 14 Oct 2026 09:12:33 +0800"
    msg["Message-ID"] = "<CAfwd-001@mail.client.example>"
    plain = (
        "Please quote this one.\n\n"
        "---------- Forwarded message ---------\n"
        "From: Wang Wei <wei.wang@buyer.example>\n"
        "Date: Tue, Oct 13, 2026 at 4:05 PM\n"
        "Subject: RFQ 316L flat bar\n"
        "To: Lin Mei <mei.lin@client.example>\n\n"
        "Hi Mei,\n\nWe need 316L flat bar 50x6mm, 2 tons, delivery to Kaohsiung by Nov 30.\n"
        "Spec sheet attached.\n\nThanks,\nWang Wei\n")
    msg.set_content(plain)
    msg.add_alternative(
        "<div>Please quote this one.</div><div class=\"gmail_quote\">---------- Forwarded "
        "message ---------<br>From: <b>Wang Wei</b> &lt;wei.wang@buyer.example&gt;<br>"
        "Date: Tue, Oct 13, 2026 at 4:05 PM<br>Subject: RFQ 316L flat bar<br></div>"
        "<p>We need 316L flat bar 50x6mm, 2 tons.</p>", subtype="html")
    msg.add_attachment(b"PK\x03\x04 fake xlsx bytes", maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       filename="../../etc/spec sheet (v2).xlsx")
    return msg.as_bytes(policy=SMTP)


def outlook_attachment() -> bytes:
    inner2 = EmailMessage()
    inner2["From"] = "Origin Mill <sales@mill.example>"
    inner2["To"] = "purchasing@supplier.example"
    inner2["Subject"] = "Mill certificate"
    inner2["Date"] = "Mon, 12 Oct 2026 08:00:00 +0000"
    inner2["Message-ID"] = "<mill-cert-1@mill.example>"
    inner2.set_content("Certificate attached.\n")
    inner2.add_attachment(b"%PDF-1.4 cert", maintype="application", subtype="pdf",
                          filename="cert.pdf")

    inner = EmailMessage()
    inner["From"] = "\"Supplier Sales\" <quotes@supplier.example>"
    inner["To"] = "mei.lin@client.example"
    inner["Subject"] = "Quotation Q-7781 for RFQ 316L flat bar"
    inner["Date"] = "Thu, 15 Oct 2026 10:30:00 +0800"
    inner["Message-ID"] = "<q7781@supplier.example>"
    inner.set_content("Dear Mei,\n\nOur price: USD 3,950/t CIF Kaohsiung, 3 weeks.\n")
    inner.add_alternative("<p>Dear Mei,</p><p>Our price: <b>USD 3,950/t</b></p>",
                          subtype="html")
    inner.add_attachment(b"%PDF-1.4 quotation", maintype="application", subtype="pdf",
                         filename="Quotation Q-7781.pdf")
    inner.add_attachment(inner2)

    outer = EmailMessage()
    outer["From"] = "Lin Mei <mei.lin@client.example>"
    outer["To"] = "contact@crontal.ai"
    outer["Subject"] = "FW: Quotation Q-7781 for RFQ 316L flat bar"
    outer["Date"] = "Thu, 15 Oct 2026 11:00:00 +0800"
    outer["Message-ID"] = "<fw-q7781@mail.client.example>"
    outer["In-Reply-To"] = "<q7781@supplier.example>"
    outer["References"] = "<rfq-thread-root@buyer.example> <q7781@supplier.example>"
    outer.set_content("FYI - supplier quote for the 316L RFQ.\n")
    outer.add_attachment(inner)
    return outer.as_bytes(policy=SMTP)


def main() -> None:
    rsa_private, rsa_record = rsa_key()
    ed_private, ed_record = ed_key()
    keys = {"sel1._domainkey.client.example": rsa_record,
            "ed1._domainkey.client.example": ed_record}
    (HERE / "gmail_inline_forward.eml").write_bytes(
        sign(gmail_inline(), "sel1", "client.example", rsa_private))
    (HERE / "outlook_attachment_forward.eml").write_bytes(
        sign(outlook_attachment(), "sel1", "client.example", rsa_private, relaxed=False))
    (HERE / "ed25519_signed.eml").write_bytes(
        sign(gmail_inline(), "ed1", "client.example", ed_private,
             algorithm=b"ed25519-sha256"))
    (HERE / "keys.json").write_text(json.dumps(keys, indent=2) + "\n")


if __name__ == "__main__":
    main()
