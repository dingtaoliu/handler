"""CSV exports via the common gateway, available to both native and Codex harnesses."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from ..artifacts import ArtifactStore
from ..context import RunContext
from ..gateway.base import ToolResult, ToolSpec
from ..spreadsheets import MAX_COLUMNS, MAX_ROWS, write_csv
from ..types import Risk

logger = logging.getLogger(__name__)


class SpreadsheetTools:
    def __init__(self, workspace_for: Callable[[str], Path], artifacts: ArtifactStore):
        self.workspace_for = workspace_for
        self.artifacts = artifacts

    async def _write(self, ctx: RunContext, args: dict) -> ToolResult:
        workspace = self.workspace_for(ctx.thread_key)
        try:
            receipt = await asyncio.to_thread(write_csv, workspace, args["path"], args["columns"],
                                              args["rows"], overwrite=args.get("overwrite", False))
        except (ValueError, OSError) as exc:
            return ToolResult(str(exc), is_error=True)
        owner = ctx.acting_as or ctx.user_id
        if owner:
            try:
                data = await asyncio.to_thread((workspace / receipt["path"]).read_bytes)
                artifact = await self.artifacts.save(owner, receipt["filename"], data, "text/csv")
            except Exception:
                logger.exception("Could not persist generated CSV artifact")
                # The workspace export is still available; don't claim delivery succeeded.
                return ToolResult({**receipt, "status": "generated", "error":
                    "Artifact storage failed. CSV remains in workspace; retry with overwrite=true "
                    "or upload it using crontal_api_call."}, is_error=True)
            receipt.update(status="ready_to_download", artifact=artifact.public())
        else:
            receipt.update(status="generated", note="No user owner for a download; upload the CSV "
                           "to Crontal Files or deliver it to an authorized user explicitly.")
        return ToolResult(receipt)

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec("write_spreadsheet", "spreadsheet.write", Risk.WRITE_INTERNAL,
            "Write a typed CSV table compatible with Excel/Google Sheets. Returns a compact receipt "
            "and a durable chat download attachment for the current user. UTF-8 BOM, proper quoting. "
            "CSV has no styles, multiple tabs or automatic formulas: supply computed scalar values. "
            "Formula-looking text is prefixed with an apostrophe (literalized_cells reports the count). "
            "Identifiers retain their original text; spreadsheet import may require text column types. "
            "Missing cells are blank; numbers/booleans/dates must match column types. "
            "Use crontal_api_call to also save the workspace CSV in Crontal Files. Do not invent sandbox links.",
            {"type": "object", "properties": {
                "path": {"type": "string"},
                "columns": {"type": "array", "minItems": 1, "maxItems": MAX_COLUMNS,
                    "items": {"type": "object", "properties": {"key": {"type": "string"},
                        "heading": {"type": "string"}, "type": {"type": "string",
                            "enum": ["text", "number", "boolean", "date"]}},
                        "required": ["key", "heading", "type"], "additionalProperties": False}},
                "rows": {"type": "array", "maxItems": MAX_ROWS, "items": {"type": "object"}},
                "overwrite": {"type": "boolean", "default": False}},
                "required": ["path", "columns", "rows"], "additionalProperties": False}, self._write)]
