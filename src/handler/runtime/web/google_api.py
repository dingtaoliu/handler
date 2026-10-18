"""Connect, inspect and disconnect the instance's Google account.

- POST   /api/connections/google/start  (admin) → {"url": Google consent URL}
         optional JSON body {"return_to": "<url>"}: where the callback sends the
         browser afterwards. Its origin must be in HANDLER_RETURN_TO_ORIGINS
         (comma-separated origins), else 400. It is never an open redirect.
- GET    /oauth/google/callback          (public; the signed state authenticates)
- GET    /api/connections/google         status for the Connections tab
- DELETE /api/connections/google/{id}    (admin) revoke at Google + delete

The browser authenticates with a bearer header, so `start` is a JSON call
and the UI then navigates to the returned URL. The callback redirects back
to `/app#connections` with `google=connected` or `google_error=...`, or, when
the flow was started with a `return_to`, to that URL with `google=connected`
or `google=error&reason=...`.
"""

from __future__ import annotations

import os
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..auth import Authenticator, Principal
from ..gateway.google_oauth import (
    CALLBACK_PATH,
    INSTANCE_OWNER,
    GoogleOAuth,
    OAuthError,
)


def request_base_url(request: Request) -> str:
    """The URL the browser used, honouring a TLS-terminating proxy (Heroku)."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0]
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") \
        or request.url.netloc
    return f"{proto.strip()}://{host.split(',')[0].strip()}"


RETURN_TO_ORIGINS_ENV = "HANDLER_RETURN_TO_ORIGINS"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_RETURN_TO = 2048


def _origin(url: str) -> str | None:
    """Normalised `scheme://host[:port]` of an absolute http(s) URL, else None.

    Rejects anything a browser might resolve differently from us: userinfo,
    backslashes, whitespace/control characters, missing host, other schemes.
    """
    if not url or len(url) > _MAX_RETURN_TO or "\\" in url \
            or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname or "@" in parts.netloc:
        return None
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def parse_origins(raw: str | Iterable[str] | None) -> frozenset[str]:
    """Allowlist entries (comma-separated string or iterable) → normalised origins.

    Entries that are not a bare origin (a path, query or fragment) are ignored.
    """
    items = raw.split(",") if isinstance(raw, str) else list(raw or ())
    origins = set()
    for item in items:
        item = item.strip().rstrip("/")
        origin = _origin(item)
        if origin and urlsplit(item).path in ("", "/") and "?" not in item \
                and "#" not in item:
            origins.add(origin)
    return frozenset(origins)


def allowed_return_to(url: str | None, origins: frozenset[str]) -> bool:
    origin = _origin(url or "")
    return origin is not None and origin in origins


def _with_query(url: str, **params: str) -> str:
    """`url` with params set (replacing any of the same name), fragment kept."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in params and k != "reason"]
    query += [(k, v) for k, v in params.items()]
    return urlunsplit(parts._replace(query=urlencode(query)))


class StartRequest(BaseModel):
    return_to: str | None = None


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def google_router(oauth: GoogleOAuth | None, auth: Authenticator, *,
                  return_to_origins: str | Iterable[str] | None = None) -> APIRouter:
    """`return_to_origins` defaults to the HANDLER_RETURN_TO_ORIGINS env var."""
    router = APIRouter()
    origins = parse_origins(os.environ.get(RETURN_TO_ORIGINS_ENV, "")
                            if return_to_origins is None else return_to_origins)
    admin = Depends(auth.require_admin)
    user = Depends(auth.require_principal)

    def configured() -> GoogleOAuth:
        if oauth is None:
            raise HTTPException(503, "Google OAuth is not configured: set "
                                "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET")
        return oauth

    @router.get("/api/connections/google")
    async def status(request: Request, _: Principal = user) -> dict:
        if oauth is None:
            return {"configured": False, "connection": None}
        connection = await oauth.connection(INSTANCE_OWNER)
        return {
            "configured": True,
            "redirect_uri": oauth.redirect_uri(request_base_url(request)),
            "requested_scopes": list(oauth.config.scopes),
            "connection": None if connection is None else {
                "id": connection.id, "account": connection.external_ref,
                "status": connection.status, "scopes": connection.scopes,
                "created_by": connection.created_by,
                "created_at": _iso(connection.created_at),
                "last_used_at": _iso(connection.last_used_at),
            },
        }

    @router.post("/api/connections/google/start")
    async def start(request: Request, body: StartRequest | None = None,
                    principal: Principal = admin) -> dict:
        google = configured()
        return_to = (body.return_to or "").strip() if body else ""
        if return_to and not allowed_return_to(return_to, origins):
            raise HTTPException(400, "return_to must be an http(s) URL whose origin is "
                                f"listed in {RETURN_TO_ORIGINS_ENV}")
        redirect_uri = google.redirect_uri(request_base_url(request))
        return {"url": google.start(started_by=principal.identity,
                                    redirect_uri=redirect_uri,
                                    return_to=return_to or None),
                "redirect_uri": redirect_uri}

    @router.get(CALLBACK_PATH)
    async def callback(state: str = "", code: str = "", error: str = "") -> RedirectResponse:
        # The state is signed, but re-check the allowlist anyway: it may have
        # shrunk since the flow started, and an unknown target falls back home.
        return_to = oauth.state_return_to(state) if oauth is not None else None
        if return_to and not allowed_return_to(return_to, origins):
            return_to = None

        def back(**query: str) -> RedirectResponse:
            if return_to:
                if "google_error" in query:
                    target = _with_query(return_to, google="error",
                                         reason=query["google_error"])
                else:
                    target = _with_query(return_to, google="connected")
                return RedirectResponse(target, status_code=303)
            return RedirectResponse(f"/app?{urlencode(query)}#connections", status_code=303)

        if oauth is None:
            return back(google_error="Google OAuth is not configured on this instance.")
        if error:  # e.g. access_denied when the user cancels
            return back(google_error=f"Google sign-in was not completed ({error}).")
        if not code or not state:
            return back(google_error="Google did not return a sign-in code.")
        try:
            connection = await oauth.complete(code, state)
        except OAuthError as exc:
            return back(google_error=str(exc))
        return back(google="connected", account=connection.external_ref or "")

    @router.delete("/api/connections/google/{connection_id}")
    async def disconnect(connection_id: str, _: Principal = admin) -> dict:
        google = configured()
        connection = await google.get_connection(connection_id)
        if connection is None:
            raise HTTPException(404, "connection not found")
        revoked = await google.disconnect(connection)
        return {"deleted": True, "revoked": revoked}

    return router
