"""Native PDF tool outputs: upload intact PDFs and persist compact file references."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import openai
from agents import ToolOutputFileContent, ToolOutputText

from ..documents.limiter import capped_read
from ..documents.pdf import prepare_input

logger = logging.getLogger(__name__)

# File types the Responses API accepts as an input file, matched on the name's
# extension, which it compares case-sensitively: "Bill.PDF" is refused with a 400
# ("Expected context stuffing file type to be a supported format") on the model
# call that carries the file, failing the whole run.
INPUT_FILE_SUFFIXES = frozenset({
    ".art", ".bat", ".brf", ".c", ".cls", ".css", ".csv", ".diff", ".doc", ".docx",
    ".dot", ".eml", ".es", ".h", ".hs", ".htm", ".html", ".hwp", ".hwpx", ".ics",
    ".ifb", ".java", ".js", ".json", ".keynote", ".ksh", ".ltx", ".mail", ".markdown",
    ".md", ".mht", ".mhtml", ".mjs", ".nws", ".odt", ".pages", ".patch", ".pdf", ".pl",
    ".pm", ".pot", ".ppa", ".pps", ".ppt", ".pptx", ".pwz", ".py", ".rst", ".rtf",
    ".scala", ".sh", ".shtml", ".srt", ".sty", ".svg", ".svgz", ".tex", ".text", ".tsv",
    ".txt", ".vcf", ".vtt", ".wiz", ".xla", ".xlb", ".xlc", ".xlm", ".xls", ".xlsx",
    ".xlt", ".xlw", ".xml", ".yaml", ".yml",
})


def input_file_name(name: str, *, pdf: bool = False) -> str | None:
    """The name to upload a file under: its extension lowercased.

    `pdf`: the bytes are a PDF, so the name always ends in ".pdf" (a selected-pages
    copy, or a PDF saved without the extension). None when the type is not one
    the API accepts as an input file; the caller must not send it as one.
    """
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem.strip():
        stem, suffix = name, ""
    stem = stem.strip() or "document"
    ext = "." + suffix.lower() if suffix else ""
    if pdf:
        return stem + ".pdf" if ext in ("", ".pdf") else f"{stem}{ext}.pdf"
    return stem + ext if ext in INPUT_FILE_SUFFIXES else None


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
        pdf = await capped_read("pdf_upload", prepare_input, path, display=display, pages=pages)
        scope = f"{self.client.base_url}|{self.client.api_key}|{self.client.organization}|{self.client.project}"
        name = input_file_name(path.name, pdf=True)
        assert name is not None  # always, for a PDF
        # The upload name is part of the key: a file stored before names were
        # normalised (e.g. "Bill.PDF", which the API refuses as input) is not
        # reused; the PDF is simply uploaded again under its accepted name.
        key = hashlib.sha256(scope.encode() + b"\0" + name.encode() + b"\0"
                             + pdf.data).hexdigest()
        async with self.lock:
            file_id = self.references.get(key)
            if not file_id:
                uploaded = await self.client.files.create(
                    file=(name, pdf.data, "application/pdf"), purpose="user_data")
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
