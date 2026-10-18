"""`GET /api/ui/manifest`: what the UI should show for this instance.

Lists the enabled integrations and the UI entries they contribute, after the
core entries every instance has. Phase 3 of the integrations refactor makes the
UI render from this; until then it is informational.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..auth import Authenticator
from ..integrations.base import UIManifestEntry
from ..integrations.registry import LoadedIntegrations

CORE_UI_ENTRIES: tuple[UIManifestEntry, ...] = (
    UIManifestEntry("nav", "chat", "Chat"),
    UIManifestEntry("nav", "tasks", "Tasks"),
    UIManifestEntry("nav", "automations", "Automations"),
    UIManifestEntry("nav", "notifications", "Notifications"),
    UIManifestEntry("nav", "settings", "Settings"),
    UIManifestEntry("thread_type", "chat", "Chat"),
    UIManifestEntry("thread_type", "automation", "Automation"),
)


def manifest(loaded: LoadedIntegrations) -> dict[str, Any]:
    body = loaded.manifest()
    body["entries"] = [e.to_dict("core") for e in CORE_UI_ENTRIES] + body["entries"]
    return body


def ui_manifest_router(loaded: LoadedIntegrations, auth: Authenticator) -> APIRouter:
    router = APIRouter()

    @router.get("/api/ui/manifest")
    async def ui_manifest(_principal=Depends(auth.require_principal)) -> dict[str, Any]:
        return manifest(loaded)

    return router
