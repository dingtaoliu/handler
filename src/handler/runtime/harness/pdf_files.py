"""Native PDF tool outputs: upload intact PDFs and persist compact file references."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import openai
from agents import ToolOutputFileContent, ToolOutputText

from ..documents.limiter import capped_read, reader_pool
from ..documents.pdf import prepare_upload

logger = logging.getLogger(__name__)


class PDFFiles:
    def __init__(self, client: openai.AsyncOpenAI, home: Path | None = None):
        self.client = client
        # Harness home is outside the agent-writable workspace and is durably synced.
        self.cache_path = home / "openai-pdf-files.json" if home else None
        self.lock = asyncio.Lock()
        self.references: dict[str, str] = {}
        if self.cache_path and self.cache_path.is_file():
            try:
                loaded = json.loads(self.cache_path.read_text())
                if isinstance(loaded, dict):
                    self.references = {k: v for k, v in loaded.items()
                                       if isinstance(k, str) and isinstance(v, str)}
            except (OSError, ValueError):
                logger.warning("Could not load PDF file-reference cache", exc_info=True)

    async def read(self, path: Path, *, display: str, pages: str | None = None
                   ) -> list[ToolOutputText | ToolOutputFileContent]:
        # The reader process checks the PDF and copies selected pages to a file;
        # here the bytes are hashed in chunks and loaded only to upload them.
        pdf = await capped_read("pdf_upload", prepare_upload, path, display=display,
                                pages=pages, scratch=reader_pool().scratch_dir())
        try:
            scope = f"{self.client.base_url}|{self.client.api_key}|{self.client.organization}|{self.client.project}"
            key = await asyncio.to_thread(_digest, scope, pdf.path)
            async with self.lock:
                file_id = self.references.get(key)
                if not file_id:
                    data = await asyncio.to_thread(pdf.path.read_bytes)
                    uploaded = await self.client.files.create(
                        file=(path.name, data, "application/pdf"), purpose="user_data")
                    del data
                    file_id = uploaded.id
                    self.references[key] = file_id
                    self._save()
        finally:
            if pdf.copy:
                pdf.path.unlink(missing_ok=True)
        return [ToolOutputText(text=pdf.description), ToolOutputFileContent(file_id=file_id)]

    def _save(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.references), encoding="utf-8")
            temporary.replace(self.cache_path)
        except OSError:
            # The uploaded file remains usable even when persistence is unavailable.
            logger.warning("Could not save PDF file-reference cache", exc_info=True)


def _digest(scope: str, path: Path) -> str:
    """sha256(scope, NUL, file bytes), read in chunks."""
    digest = hashlib.sha256(scope.encode() + b"\0")
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
