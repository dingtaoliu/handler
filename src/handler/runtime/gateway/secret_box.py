"""Encryption for stored secrets and signing for OAuth state.

Connection secrets (OAuth refresh tokens) are encrypted with Fernet before
they reach the store, so a database dump alone doesn't leak them. The key is
derived from `HANDLER_SECRET_KEY` (any string); instances that don't set it
fall back to `HANDLER_ADMIN_KEY`. Changing the key makes stored secrets
unreadable: the connection then reports "needs reconnect".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..store.base import SecretStore


class SecretUnreadable(Exception):
    """A stored secret could not be decrypted (wrong or rotated key)."""


def _derive(key: str, purpose: str) -> bytes:
    return hashlib.sha256(f"handler:{purpose}:{key}".encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SecretBox:
    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("secret key must not be empty")
        self._fernet = Fernet(base64.urlsafe_b64encode(_derive(key, "secrets")))
        self._signing = _derive(key, "signing")

    @classmethod
    def from_env(cls, env=None) -> SecretBox | None:
        env = os.environ if env is None else env
        key = env.get("HANDLER_SECRET_KEY") or env.get("HANDLER_ADMIN_KEY")
        return cls(key) if key else None

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise SecretUnreadable("stored secret cannot be decrypted") from exc

    # ---- signed, expiring payloads (OAuth state) -------------------------

    def sign(self, payload: dict[str, Any], ttl_seconds: float,
             now: float | None = None) -> str:
        body = {**payload, "exp": int((now or time.time()) + ttl_seconds)}
        data = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
        mac = _b64(hmac.new(self._signing, data.encode(), hashlib.sha256).digest())
        return f"{data}.{mac}"

    def verify(self, token: str, now: float | None = None) -> dict[str, Any] | None:
        """The payload if the signature is valid and not expired, else None."""
        data, _, mac = (token or "").partition(".")
        if not data or not mac:
            return None
        expected = _b64(hmac.new(self._signing, data.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(mac, expected):
            return None
        try:
            payload = json.loads(_unb64(data))
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("exp", 0) < (now or time.time()):
            return None
        return payload


class EncryptedSecrets:
    """A SecretStore view that encrypts values on the way in and out."""

    def __init__(self, store: SecretStore, box: SecretBox) -> None:
        self._store = store
        self._box = box

    async def put(self, key: str, value: str) -> None:
        await self._store.put_secret(key, self._box.encrypt(value))

    async def get(self, key: str) -> str | None:
        stored = await self._store.get_secret(key)
        return None if stored is None else self._box.decrypt(stored)

    async def delete(self, key: str) -> bool:
        return await self._store.delete_secret(key)
