"""Google OAuth completion tool — lets the agent finish an in-progress auth flow."""

from __future__ import annotations

from pathlib import Path

from agents import function_tool

from ..google_oauth import pop_pending_flow, exchange_console_authorization


@function_tool
def complete_google_auth(service: str, code_or_url: str) -> str:
    """Complete a pending Google OAuth authorization flow.

    Use this after the gmail or google_drive tool has provided an auth URL and
    the user has opened it and copied the redirect URL (or code) from their browser.

    Args:
        service: The service to authorize — 'gmail' or 'gdrive'
        code_or_url: The redirect URL (http://localhost:...?code=...) or raw authorization code
    """
    if service not in ("gmail", "gdrive"):
        return "service must be 'gmail' or 'gdrive'"

    entry = pop_pending_flow(service)
    if entry is None:
        return (
            f"No pending auth flow for {service}. "
            f"Use the {service} tool first — it will start the auth flow and provide the URL to open."
        )

    user_id, flow = entry
    try:
        creds = exchange_console_authorization(flow, code_or_url)
    except Exception as e:
        return f"Authorization failed: {e}"

    if service == "gmail":
        from .gmail import _token_path
    else:
        from .gdrive import _token_path

    token_file = Path(_token_path(user_id))
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())

    service_name = "Gmail" if service == "gmail" else "Google Drive"
    return f"{service_name} authorization complete. You can now use the {service} tool."
