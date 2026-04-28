"""Shared helpers for Google installed-app OAuth flows."""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import contextmanager


def _pick_console_redirect_uri(client_config: Mapping[str, object]) -> str:
    redirect_uris = [
        str(value).strip()
        for value in client_config.get("redirect_uris", [])
        if str(value).strip()
    ]
    if not redirect_uris:
        raise ValueError("OAuth client is missing redirect_uris")

    for prefix in (
        "http://localhost",
        "http://127.0.0.1",
        "https://localhost",
        "https://127.0.0.1",
    ):
        for redirect_uri in redirect_uris:
            if redirect_uri.startswith(prefix):
                return redirect_uri

    return redirect_uris[0]


def build_console_authorization_url(flow) -> str:
    """Return an auth URL for a copy-paste installed-app flow.

    The google-auth-oauthlib library still generates the URL, but this version
    requires callers to set redirect_uri explicitly before authorization_url().
    """

    if not flow.redirect_uri:
        flow.redirect_uri = _pick_console_redirect_uri(flow.client_config)
    auth_url, _ = flow.authorization_url(prompt="consent")
    return auth_url


@contextmanager
def _allow_insecure_localhost_callback(authorization_response: str):
    previous = os.environ.get("OAUTHLIB_INSECURE_TRANSPORT")
    use_insecure_transport = authorization_response.startswith(
        ("http://localhost", "http://127.0.0.1")
    )

    if use_insecure_transport:
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    try:
        yield
    finally:
        if use_insecure_transport:
            if previous is None:
                os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
            else:
                os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = previous


def exchange_console_authorization(flow, authorization_response: str):
    """Exchange a pasted redirect URL or raw code for OAuth credentials."""

    value = authorization_response.strip()
    if not value:
        raise ValueError("authorization response is required")

    if "://" in value:
        with _allow_insecure_localhost_callback(value):
            flow.fetch_token(authorization_response=value)
    else:
        flow.fetch_token(code=value)
    return flow.credentials
