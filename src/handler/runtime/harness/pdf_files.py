"""Native PDF tool outputs: upload intact PDFs and persist compact file references."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import openai
from agents import ToolOutputFileContent, ToolOutputText

from ..documents.pdf import prepare_input

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
        pdf = await asyncio.to_thread(prepare_input, path, display=display, pages=pages)
        scope = f"{self.client.base_url}|{self.client.api_key}|{self.client.organization}|{self.client.project}"
        key = hashlib.sha256(scope.encode() + b"\0" + pdf.data).hexdigest()
        async with self.lock:
            file_id = self.references.get(key)
            if not file_id:
                uploaded = await self.client.files.create(
                    file=(path.name, pdf.data, "application/pdf"), purpose="user_data")
                file_id = uploaded.id
                self.references[key] = file_id
                self._save()
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
