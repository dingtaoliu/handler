"""Handler's own Google OAuth flow and the `google` connection's tokens.

Interactive users connect their own Google account (owner ``user:<id>``).
The instance owner remains available for system-triggered work and backward
compatibility with operator flows that do not act as a Handler user.

- `start()` signs a short-lived `state` (who started it, the redirect URI,
  a nonce) and returns Google's consent URL.
- `complete()` checks the state (signature, expiry, single use), exchanges
  the code, and stores a `connections` row whose `secret_ref` points at the
  Fernet-encrypted refresh token in the `secrets` table.
- `resolve()` (the CredentialProvider contract) returns a bearer header for
  the Gmail API, refreshing the access token when it is near expiry. A refresh
  that Google answers with `invalid_grant` (revoked, expired, password reset)
  marks the connection `needs_reauth` and raises NeedsReauth.
- `disconnect()` revokes the grant at Google and deletes the row and secret.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, replace
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from ..types import Connection
from .credentials import ConnectionCredentials, NeedsReauth, UpstreamAccess
from .secret_box import EncryptedSecrets, SecretBox, SecretUnreadable

log = logging.getLogger(__name__)

CONNECTOR = "google"
INSTANCE_OWNER = "instance"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_FULL = "https://mail.google.com/"
GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
# Email-focused default. gmail.modify covers search, read, attachments and
# drafts and send (labels: no tool exposes them), but not permanent delete.
# What the agent can do is decided by the tool set and grants, not the token.
DEFAULT_SCOPES = ("openid", USERINFO_EMAIL, GMAIL_MODIFY)
# Any one of these is enough for a capability (Gmail's scopes nest).
READ_SCOPES = (GMAIL_FULL, GMAIL_MODIFY, GMAIL_READONLY)
DRAFT_SCOPES = (GMAIL_FULL, GMAIL_MODIFY, GMAIL_COMPOSE)
SEND_SCOPES = (GMAIL_FULL, GMAIL_MODIFY, GMAIL_COMPOSE, GMAIL_SEND)
CALLBACK_PATH = "/oauth/google/callback"
STATE_TTL_SECONDS = 600
REFRESH_MARGIN_SECONDS = 120


def user_owner(user_id: str) -> str:
    """Stable connection owner for a Handler user."""
    return f"user:{user_id}"


class OAuthError(Exception):
    """The OAuth flow or a token call failed; the message is safe to show."""


@dataclass(frozen=True)
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    public_url: str | None = None  # e.g. https://crontal-agent-....herokuapp.com
    scopes: tuple[str, ...] = DEFAULT_SCOPES

    @classmethod
    def from_env(cls, env=None) -> GoogleOAuthConfig | None:
        env = os.environ if env is None else env
        client_id = env.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        client_secret = env.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            return None
        public = env.get("HANDLER_PUBLIC_URL", "").strip().rstrip("/") or None
        scopes = tuple(env.get("GOOGLE_OAUTH_SCOPES", "").split()) or DEFAULT_SCOPES
        return cls(client_id, client_secret, public, scopes)


def _jwt_claims(token: str | None) -> dict[str, Any]:
    """Claims of an id_token received directly from Google's token endpoint.

    No signature check: the token came over TLS straight from Google in
    response to our client-authenticated code exchange (OIDC Core 3.1.3.7).
    """
    try:
        payload = (token or "").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("error", "")) if isinstance(body, dict) else ""


class GoogleOAuth:
    def __init__(
        self, config: GoogleOAuthConfig, store: Any, box: SecretBox, *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._store = store
        self._box = box
        self._secrets = EncryptedSecrets(store, box)
        self._client = client or httpx.AsyncClient(timeout=30)
        self._clock = clock
        self._tokens: dict[str, tuple[str, float]] = {}  # connection id → (token, expiry)
        self._locks: dict[str, asyncio.Lock] = {}
        self._used_nonces: dict[str, float] = {}
        self._connections = ConnectionCredentials(store, {"local": self._access})

    # ---- the consent flow ------------------------------------------------

    def redirect_uri(self, request_base_url: str) -> str:
        """`<public url>/oauth/google/callback`; HANDLER_PUBLIC_URL wins over the request."""
        base = self.config.public_url or request_base_url.rstrip("/")
        return base + CALLBACK_PATH

    def start(self, *, started_by: str, redirect_uri: str,
              owner: str = INSTANCE_OWNER, return_to: str | None = None) -> str:
        """Google's consent URL. `return_to` (already checked against the
        allowlist by the caller) is signed into the state for the callback."""
        payload = {"n": secrets.token_urlsafe(16), "by": started_by, "ru": redirect_uri,
                   "o": owner}
        if return_to:
            payload["rt"] = return_to
        state = self._box.sign(payload, STATE_TTL_SECONDS, now=self._clock())
        query = {
            "client_id": self.config.client_id, "redirect_uri": redirect_uri,
            "response_type": "code", "scope": " ".join(self.config.scopes),
            "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true", "state": state,
        }
        return f"{AUTH_URL}?{urlencode(query)}"

    def state_return_to(self, state: str) -> str | None:
        """The `return_to` signed into a (valid, unexpired) state, else None.

        Does not consume the state's nonce, so it can be read before or after
        `complete()` and on error callbacks.
        """
        payload = self._box.verify(state, now=self._clock()) if state else None
        value = payload.get("rt") if payload else None
        return value if isinstance(value, str) and value else None

    def _check_state(self, state: str) -> dict[str, Any]:
        now = self._clock()
        payload = self._box.verify(state, now=now)
        if payload is None:
            raise OAuthError("The sign-in link is invalid or expired. Start again from "
                             "Connections.")
        self._used_nonces = {n: exp for n, exp in self._used_nonces.items() if exp > now}
        if payload["n"] in self._used_nonces:
            raise OAuthError("This sign-in link was already used. Start again from "
                             "Connections.")
        self._used_nonces[payload["n"]] = float(payload["exp"])
        return payload

    async def complete(self, code: str, state: str) -> Connection:
        payload = self._check_state(state)
        response = await self._client.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": payload["ru"], "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
        })
        if response.status_code != 200:
            raise OAuthError(f"Google refused the code exchange "
                             f"({_error_code(response) or response.status_code}).")
        tokens = response.json()
        # Store what Google actually granted (users can untick boxes); each tool
        # checks for the scope it needs and asks for a reconnect when it's missing.
        granted = set((tokens.get("scope") or "").split())
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            await self._revoke(tokens.get("access_token"))
            raise OAuthError("Google returned no refresh token. Connect again.")
        email = _jwt_claims(tokens.get("id_token")).get("email") or await self._userinfo_email(
            tokens["access_token"])
        owner = payload["o"]
        connection = Connection(
            connector=CONNECTOR, owner=owner, provider="local", external_ref=email,
            scopes=sorted(granted), status="active", created_by=payload["by"])
        secret_ref = f"connection:{connection.id}:refresh_token"
        connection = replace(connection, secret_ref=secret_ref)
        await self._secrets.put(secret_ref, refresh_token)
        await self._store.put_connection(connection)
        # Replace earlier google connections for this owner. Don't revoke them:
        # revoking would also revoke the grant just made for the same account.
        for old in await self._store.list_connections(owner):
            if old.connector == CONNECTOR and old.id != connection.id:
                await self._forget(old)
        self._tokens[connection.id] = (
            tokens["access_token"], self._clock() + float(tokens.get("expires_in", 3600)))
        log.info("google connected", extra={"owner": owner, "account": email,
                                             "by": payload["by"]})
        return connection

    async def _userinfo_email(self, access_token: str) -> str | None:
        try:
            response = await self._client.get(
                USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
            return response.json().get("email") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    # ---- tokens (CredentialProvider) -------------------------------------

    async def resolve(self, connector: str, owner: str) -> UpstreamAccess | None:
        if connector != CONNECTOR:
            return None
        return await self._connections.resolve(connector, owner)

    async def mark_needs_reauth(self, connector: str, owner: str) -> None:
        connection = await self._store.find_connection(connector, owner)
        if connection is not None:
            await self._needs_reauth(connection)

    def invalidate(self, owner: str = INSTANCE_OWNER) -> None:
        """Drop cached access tokens so the next resolve refreshes (after a 401)."""
        self._tokens.clear()

    async def account(self, owner: str = INSTANCE_OWNER) -> str | None:
        connection = await self._store.find_connection(CONNECTOR, owner)
        return connection.external_ref if connection else None

    async def granted_scopes(self, owner: str = INSTANCE_OWNER) -> list[str] | None:
        """Scopes on the active connection; None when there is none."""
        connection = await self._store.find_connection(CONNECTOR, owner)
        return list(connection.scopes) if connection else None

    async def _access(self, connection: Connection) -> UpstreamAccess:
        lock = self._locks.setdefault(connection.id, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(connection.id)
            if cached is None or cached[1] - self._clock() < REFRESH_MARGIN_SECONDS:
                cached = await self._refresh(connection)
                self._tokens[connection.id] = cached
        return UpstreamAccess(url="https://gmail.googleapis.com",
                              headers={"Authorization": f"Bearer {cached[0]}"})

    async def _refresh(self, connection: Connection) -> tuple[str, float]:
        try:
            refresh_token = (await self._secrets.get(connection.secret_ref)
                             if connection.secret_ref else None)
        except SecretUnreadable:
            refresh_token = None
            log.warning("google refresh token unreadable (secret key changed?)",
                        extra={"connection": connection.id})
        if not refresh_token:
            await self._needs_reauth(connection)
            raise NeedsReauth("stored Google credentials are missing")
        try:
            response = await self._client.post(TOKEN_URL, data={
                "grant_type": "refresh_token", "refresh_token": refresh_token,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
            })
        except httpx.HTTPError as exc:
            raise OAuthError(f"Could not reach Google to refresh the token: {exc}") from exc
        if response.status_code == 200:
            body = response.json()
            return body["access_token"], self._clock() + float(body.get("expires_in", 3600))
        code = _error_code(response)
        if code == "invalid_grant":
            await self._needs_reauth(connection)
            raise NeedsReauth("Google rejected the refresh token (invalid_grant)")
        if code in ("invalid_client", "unauthorized_client"):
            raise OAuthError(f"Google rejected handler's OAuth client ({code}); check "
                             "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET.")
        raise OAuthError(f"Google token refresh failed (HTTP {response.status_code} {code})")

    async def _needs_reauth(self, connection: Connection) -> None:
        self._tokens.pop(connection.id, None)
        current = await self._store.get_connection(connection.id) or connection
        if current.status != "needs_reauth":
            await self._store.put_connection(replace(current, status="needs_reauth"))
            log.warning("google connection needs reconnect",
                        extra={"connection": connection.id, "account": connection.external_ref})

    # ---- status and disconnect ---------------------------------------------

    async def get_connection(self, connection_id: str) -> Connection | None:
        connection = await self._store.get_connection(connection_id)
        return connection if connection and connection.connector == CONNECTOR else None

    async def connection(self, owner: str = INSTANCE_OWNER) -> Connection | None:
        """The newest google connection for owner, whatever its status."""
        found = [c for c in await self._store.list_connections(owner)
                 if c.connector == CONNECTOR]
        return found[-1] if found else None

    async def disconnect(self, connection: Connection) -> bool:
        """Revoke at Google (best effort), then delete. True when Google confirmed."""
        try:
            token = (await self._secrets.get(connection.secret_ref)
                     if connection.secret_ref else None)
        except SecretUnreadable:
            token = None
        revoked = await self._revoke(token) if token else False
        await self._forget(connection)
        log.info("google disconnected", extra={"connection": connection.id,
                                                "revoked": revoked})
        return revoked

    async def _revoke(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            response = await self._client.post(REVOKE_URL, data={"token": token})
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def _forget(self, connection: Connection) -> None:
        self._tokens.pop(connection.id, None)
        if connection.secret_ref:
            await self._store.delete_secret(connection.secret_ref)
        await self._store.delete_connection(connection.id)


def google_oauth_from_env(store: Any, *, env=None) -> GoogleOAuth | None:
    """GoogleOAuth when GOOGLE_OAUTH_CLIENT_ID/SECRET and a secret key are set."""
    env = os.environ if env is None else env
    config = GoogleOAuthConfig.from_env(env)
    if config is None:
        return None
    box = SecretBox.from_env(env)
    if box is None:
        log.error("Google OAuth configured but neither HANDLER_SECRET_KEY nor "
                  "HANDLER_ADMIN_KEY is set to encrypt tokens; Gmail tools off")
        return None
    if not env.get("HANDLER_SECRET_KEY"):
        log.warning("HANDLER_SECRET_KEY not set; encrypting Google tokens with a key "
                    "derived from HANDLER_ADMIN_KEY (rotating it forces a reconnect)")
    return GoogleOAuth(config, store, box)
