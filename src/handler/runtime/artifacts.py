"""Immutable generated artifacts stored durably behind authenticated downloads."""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict
from typing import Any

from .files import FileStore
from .uploads import UploadedFile, sanitize_filename

_ID = re.compile(r"[0-9a-f]{32}")


class ArtifactStore:
    def __init__(self, files: FileStore):
        self.files = files

    async def save(self, owner: str, filename: str, data: bytes, content_type: str) -> UploadedFile:
        file_id = secrets.token_hex(16)
        meta = UploadedFile(file_id, owner, sanitize_filename(filename), len(data), content_type)
        # Publish metadata only after its bytes exist; an incomplete write is not downloadable.
        await self.files.put(f"artifacts/{file_id}/data", data, content_type)
        await self.files.put(f"artifacts/{file_id}/meta.json", json.dumps(asdict(meta)).encode(),
                             "application/json")
        return meta

    async def get(self, file_id: str) -> UploadedFile | None:
        if not _ID.fullmatch(file_id):
            return None
        data = await self.files.get(f"artifacts/{file_id}/meta.json")
        if data is None:
            return None
        try:
            meta = UploadedFile(**json.loads(data))
            return meta if meta.id == file_id else None
        except (ValueError, TypeError):
            return None

    async def data(self, file_id: str) -> bytes | None:
        if not _ID.fullmatch(file_id):
            return None
        return await self.files.get(f"artifacts/{file_id}/data")


def artifact_block(output: Any) -> dict | None:
    """Decode our own tool's receipt through native/CLI MCP content wrappers."""
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return None
    if isinstance(output, list):
        for part in output:
            if block := artifact_block(part):
                return block
    if isinstance(output, dict):
        artifact = output.get("artifact")
        if isinstance(artifact, dict) and _ID.fullmatch(str(artifact.get("id", ""))):
            return {"type": "file", "file_id": artifact["id"],
                    "filename": sanitize_filename(artifact.get("filename")),
                    "content_type": "text/csv"}
        for key in ("text", "content"):
            if key in output and (block := artifact_block(output[key])):
                return block
    return None
