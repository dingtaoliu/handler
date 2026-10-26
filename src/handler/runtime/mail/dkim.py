"""DKIM signature verification (RFC 6376, RFC 8463) over a raw RFC 822 message.

Cloudflare Email Routing checks SPF/DKIM/DMARC at its MX but does not hand a
trustworthy verdict to an Email Worker (see docs/rebuild/email-intake.md), so
handler verifies the forwarder's DKIM signature itself. The Worker passes the
message bytes through unchanged, so the original signatures still verify.

Supported: rsa-sha256 and ed25519-sha256, simple/relaxed canonicalization,
the l= body length tag and x= expiry. rsa-sha1 and keys under 1024 bits fail.
Public keys come from a resolver (DNS TXT); the default uses DNS over HTTPS.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

logger = logging.getLogger("handler.runtime.mail.dkim")

# name → TXT strings (each a whole record, chunks already joined). Raises
# LookupError on a temporary failure; returns [] when the name has no record.
TxtResolver = Callable[[str], Awaitable[list[str]]]

MAX_SIGNATURES = 5
_WSP = re.compile(rb"[ \t]+")
_FWS = re.compile(r"\s+")


@dataclass(frozen=True)
class DkimResult:
    domain: str
    selector: str
    ok: bool
    reason: str = ""


def split_message(raw: bytes) -> tuple[list[tuple[str, bytes]], bytes]:
    """Headers as (name, raw field incl. folding and trailing CRLF) and the body.

    Bare LF line endings are normalized to CRLF first.
    """
    data = re.sub(rb"(?<!\r)\n", b"\r\n", raw)
    end = data.find(b"\r\n\r\n")
    if end < 0:
        head, body = data, b""
        if not head.endswith(b"\r\n"):
            head += b"\r\n"
    else:
        head, body = data[: end + 2], data[end + 4:]
    fields: list[tuple[str, bytes]] = []
    for line in head.split(b"\r\n")[:-1]:
        if line[:1] in (b" ", b"\t") and fields:
            name, value = fields[-1]
            fields[-1] = (name, value + line + b"\r\n")
        elif b":" in line:
            fields.append((line.split(b":", 1)[0].decode("latin-1").strip(), line + b"\r\n"))
    return fields, body


def _canon_header(field: bytes, relaxed: bool) -> bytes:
    if not relaxed:
        return field
    name, _, value = field.partition(b":")
    value = value.replace(b"\r\n", b"")
    value = _WSP.sub(b" ", value).strip(b" \t")
    return name.strip(b" \t").lower() + b":" + value + b"\r\n"


def _canon_body(body: bytes, relaxed: bool) -> bytes:
    if relaxed:
        lines = [_WSP.sub(b" ", line).rstrip(b" \t") for line in body.split(b"\r\n")]
        body = b"\r\n".join(lines)
        while body.endswith(b"\r\n"):
            body = body[:-2]
        return body + b"\r\n" if body else b""
    while body.endswith(b"\r\n\r\n"):
        body = body[:-2]
    return body if body.endswith(b"\r\n") else body + b"\r\n"


def parse_tags(value: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for item in value.split(";"):
        if "=" not in item:
            continue
        key, _, val = item.partition("=")
        tags[key.strip()] = val.strip()
    return tags


def _strip_b(field: bytes) -> bytes:
    """The DKIM-Signature field with the b= tag's value removed."""
    return re.sub(rb"(\bb[ \t\r\n]*=)[^;]*", rb"\1", field, count=1)


def _load_key(record: str) -> tuple[str, object] | str:
    tags = parse_tags(record)
    if tags.get("v", "DKIM1") != "DKIM1":
        return "key record is not DKIM1"
    key_type = tags.get("k", "rsa").lower()
    data = _FWS.sub("", tags.get("p", ""))
    if not data:
        return "key revoked"
    try:
        der = base64.b64decode(data)
    except ValueError:
        return "key not base64"
    try:
        if key_type == "ed25519":
            return key_type, ed25519.Ed25519PublicKey.from_public_bytes(der)
        try:
            key = serialization.load_der_public_key(der)
        except ValueError:
            # Some publishers put a bare PKCS#1 RSAPublicKey in p=.
            key = serialization.load_der_public_key(
                _pkcs1_to_spki(der))
        if not isinstance(key, rsa.RSAPublicKey):
            return "key is not RSA"
        if key.key_size < 1024:
            return "RSA key shorter than 1024 bits"
        return key_type, key
    except (ValueError, TypeError) as exc:
        return f"bad key: {exc}"


def _pkcs1_to_spki(der: bytes) -> bytes:
    def length(n: int) -> bytes:
        if n < 0x80:
            return bytes([n])
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return bytes([0x80 | len(raw)]) + raw

    algorithm = bytes.fromhex("300d06092a864886f70d0101010500")
    bit_string = b"\x03" + length(len(der) + 1) + b"\x00" + der
    inner = algorithm + bit_string
    return b"\x30" + length(len(inner)) + inner


async def verify_signature(
    fields: list[tuple[str, bytes]], body: bytes, signature: bytes,
    resolve: TxtResolver, *, now: float | None = None,
) -> DkimResult:
    text = signature.split(b":", 1)[1].decode("latin-1")
    tags = parse_tags(text.replace("\r\n", ""))
    domain = tags.get("d", "").lower().rstrip(".")
    selector = tags.get("s", "")

    def fail(reason: str) -> DkimResult:
        return DkimResult(domain, selector, False, reason)

    if tags.get("v") != "1":
        return fail("unsupported DKIM version")
    algorithm = tags.get("a", "").lower()
    if algorithm not in ("rsa-sha256", "ed25519-sha256"):
        return fail(f"unsupported algorithm {algorithm or '(none)'}")
    if not domain or not selector or "b" not in tags or "bh" not in tags or "h" not in tags:
        return fail("missing required tag")
    signed = [h.strip().lower() for h in tags["h"].split(":") if h.strip()]
    if "from" not in signed:
        return fail("From header not signed")
    if "x" in tags:
        try:
            if int(tags["x"]) < (time.time() if now is None else now):
                return fail("signature expired")
        except ValueError:
            return fail("bad x= tag")
    canon = tags.get("c", "simple/simple").lower()
    header_canon, _, body_canon = canon.partition("/")
    header_relaxed = header_canon == "relaxed"
    body_relaxed = (body_canon or "simple") == "relaxed"

    canon_body = _canon_body(body, body_relaxed)
    if "l" in tags:
        try:
            length = int(tags["l"])
        except ValueError:
            return fail("bad l= tag")
        # Text after l= bytes is unsigned: anyone could append to a signed
        # message, so a signature that doesn't cover the whole body fails.
        if length < len(canon_body):
            return fail("l= does not cover the whole body")
        canon_body = canon_body[:length]
    body_hash = base64.b64encode(hashlib.sha256(canon_body).digest()).decode()
    if body_hash != _FWS.sub("", tags["bh"]):
        return fail("body hash mismatch")

    # Each listed name takes the next unused instance from the bottom up.
    remaining: dict[str, list[bytes]] = {}
    for name, field in fields:
        remaining.setdefault(name.lower(), []).append(field)
    data = b""
    for name in signed:
        instances = remaining.get(name)
        if instances:
            data += _canon_header(instances.pop(), header_relaxed)
    data += _canon_header(_strip_b(signature), header_relaxed).removesuffix(b"\r\n")

    try:
        records = await resolve(f"{selector}._domainkey.{domain}")
    except LookupError as exc:
        return fail(f"key lookup failed (temporary): {exc}")
    records = [r for r in records if "p=" in r]
    if not records:
        return fail("no key record")
    loaded = _load_key(records[0])
    if isinstance(loaded, str):
        return fail(loaded)
    key_type, key = loaded
    try:
        sig = base64.b64decode(_FWS.sub("", tags["b"]))
    except ValueError:
        return fail("signature not base64")
    try:
        if algorithm == "ed25519-sha256":
            if key_type != "ed25519":
                return fail("key type does not match algorithm")
            key.verify(sig, hashlib.sha256(data).digest())  # type: ignore[union-attr]
        else:
            if key_type != "rsa":
                return fail("key type does not match algorithm")
            key.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
    except InvalidSignature:
        return fail("signature does not verify")
    return DkimResult(domain, selector, True)


async def verify_message(raw: bytes, resolve: TxtResolver, *,
                         now: float | None = None) -> list[DkimResult]:
    """Verify every DKIM-Signature on the message (at most MAX_SIGNATURES)."""
    fields, body = split_message(raw)
    signatures = [f for name, f in fields if name.lower() == "dkim-signature"]
    results = []
    for signature in signatures[:MAX_SIGNATURES]:
        try:
            results.append(await verify_signature(fields, body, signature, resolve, now=now))
        except Exception as exc:  # noqa: BLE001 - one bad signature must not abort
            logger.warning("dkim verification crashed", extra={"error": str(exc)})
            results.append(DkimResult("", "", False, f"error: {exc}"))
    return results


DOH_ENDPOINTS = ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")


def doh_resolver(client: httpx.AsyncClient | None = None, *,
                 endpoints: tuple[str, ...] = DOH_ENDPOINTS,
                 timeout: float = 5.0) -> TxtResolver:
    """TXT lookups over DNS-over-HTTPS JSON (no resolver library needed)."""
    http = client or httpx.AsyncClient(timeout=timeout)

    async def resolve(name: str) -> list[str]:
        errors = []
        for endpoint in endpoints:
            try:
                response = await http.get(endpoint, params={"name": name, "type": "TXT"},
                                          headers={"accept": "application/dns-json"})
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{endpoint}: {exc}")
                continue
            status = body.get("Status")
            if status == 3:  # NXDOMAIN
                return []
            if status not in (0, None):
                errors.append(f"{endpoint}: DNS status {status}")
                continue
            return [_join_txt(a.get("data", "")) for a in body.get("Answer") or []
                    if a.get("type") == 16]
        raise LookupError("; ".join(errors) or "no resolver answered")

    return resolve


def _join_txt(data: str) -> str:
    """'"v=DKIM1; p=AB" "CD"' → 'v=DKIM1; p=ABCD' (DoH returns quoted chunks)."""
    chunks = re.findall(r'"((?:[^"\\]|\\.)*)"', data)
    if not chunks:
        return data
    return "".join(c.replace('\\"', '"').replace("\\\\", "\\") for c in chunks)
