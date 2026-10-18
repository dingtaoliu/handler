"""Connect, inspect and disconnect the instance's Google account.

- POST   /api/connections/google/start  (admin) → {"url": Google consent URL}
- GET    /oauth/google/callback          (public; the signed state authenticates)
- GET    /api/connections/google         status for the Connections tab
- DELETE /api/connections/google/{id}    (admin) revoke at Google + delete

The browser authenticates with a bearer header, so `start` is a JSON call
and the UI then navigates to the returned URL. The callback redirects back
to `/app#connections` with `google=connected` or `google_error=...`.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

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


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def google_router(oauth: GoogleOAuth | None, auth: Authenticator) -> APIRouter:
    router = APIRouter()
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
    async def start(request: Request, principal: Principal = admin) -> dict:
        google = configured()
        redirect_uri = google.redirect_uri(request_base_url(request))
        return {"url": google.start(started_by=principal.identity,
                                    redirect_uri=redirect_uri),
                "redirect_uri": redirect_uri}

    @router.get(CALLBACK_PATH)
    async def callback(state: str = "", code: str = "", error: str = "") -> RedirectResponse:
        def back(**query: str) -> RedirectResponse:
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
