"""Memory: manages agent-controlled knowledge files in data/memory/.

Files are stored as plain markdown (.md) on disk. An index is maintained at
data/memory/.index.json mapping filename → one-line summary for efficient
inclusion in the system prompt without loading all file contents each turn.

If the total size of all memory files is small (< FULL_LOAD_THRESHOLD chars),
the full content of every file is included in the prompt. Above the threshold,
only the index is included and the agent uses recall_memory() to load files.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("handler.memory")


def _validate_filename(name: str) -> str:
    """Sanitize and validate a memory filename.

    Strips path components, ensures .md extension, rejects path traversal
    and non-alphanumeric characters. Raises ValueError on invalid input.
    """
    name = Path(name).name  # strip any directory components
    if not name or ".." in name:
        raise ValueError(f"Invalid memory filename: {name!r}")
    if not name.endswith(".md"):
        name += ".md"
    if not re.match(r"^[\w][\w\-\.]*\.md$", name) or len(name) > 120:
        raise ValueError(f"Invalid memory filename: {name!r}")
    return name

_INDEX_FILE = ".index.json"
_MTIME_FILE = ".index_mtime.json"
FULL_LOAD_THRESHOLD = 6_000  # chars — below this, include all file content in prompt


class Memory:
    """Manages the agent-controlled memory directory (data/memory/*.md)."""

    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _index_path(self) -> Path:
        return self.memory_dir / _INDEX_FILE

    @property
    def _mtime_path(self) -> Path:
        return self.memory_dir / _MTIME_FILE

    # ------------------------------------------------------------------
    # Internal index helpers
    # ------------------------------------------------------------------

    def _load_index(self) -> dict[str, str]:
        try:
            return json.loads(self._index_path.read_text())
        except Exception:
            return {}

    def _save_index(self, index: dict[str, str]) -> None:
        self._index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False))

    def _load_mtimes(self) -> dict[str, str]:
        try:
            return json.loads(self._mtime_path.read_text())
        except Exception:
            return {}

    def _save_mtimes(self, mtimes: dict[str, str]) -> None:
        self._mtime_path.write_text(json.dumps(mtimes))

    @staticmethod
    def _derive_summary(content: str) -> str:
        """Derive a one-line summary from file content.

        Uses the first `# Heading` if present, otherwise the first non-empty line.
        """
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("# "):
                return line[2:].strip()[:120]
            return line[:120]
        return "(empty)"

    def sync_index(self) -> None:
        """Rebuild index entries for any files added or changed since last sync.

        Safe to call every turn — only rewrites the index when something changed.
        """
        index = self._load_index()
        mtimes = self._load_mtimes()
        md_files = {f.name: f for f in self.memory_dir.glob("*.md")}
        changed = False

        # Add/update entries for new or modified files
        for name, path in md_files.items():
            mtime = str(path.stat().st_mtime)
            if mtimes.get(name) != mtime:
                try:
                    content = path.read_text().strip()
                    index[name] = self._derive_summary(content)
                    mtimes[name] = mtime
                    changed = True
                except Exception:
                    pass

        # Remove entries for deleted files
        for name in list(index.keys()):
            if name not in md_files:
                del index[name]
                mtimes.pop(name, None)
                changed = True

        if changed:
            self._save_index(index)
            self._save_mtimes(mtimes)

    def _update_entry(self, filename: str, content: str) -> None:
        """Immediately update a single index entry after a write."""
        index = self._load_index()
        mtimes = self._load_mtimes()
        index[filename] = self._derive_summary(content)
        path = self.memory_dir / filename
        try:
            mtimes[filename] = str(path.stat().st_mtime)
        except Exception:
            pass
        self._save_index(index)
        self._save_mtimes(mtimes)

    def _remove_entry(self, filename: str) -> None:
        """Immediately remove a single index entry after a delete."""
        index = self._load_index()
        mtimes = self._load_mtimes()
        index.pop(filename, None)
        mtimes.pop(filename, None)
        self._save_index(index)
        self._save_mtimes(mtimes)

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def list_files(self) -> list[dict]:
        """Return metadata for all memory files, sorted by name."""
        self.sync_index()
        index = self._load_index()
        result = []
        for f in sorted(self.memory_dir.glob("*.md")):
            size = f.stat().st_size
            result.append(
                {
                    "filename": f.name,
                    "summary": index.get(f.name, ""),
                    "size": size,
                }
            )
        return result

    def read(self, filename: str) -> str:
        """Read the full content of a memory file."""
        filename = _validate_filename(filename)
        path = self.memory_dir / filename
        if not path.exists():
            return ""
        return path.read_text()

    def write(self, filename: str, content: str) -> None:
        """Write a memory file and immediately update the index."""
        filename = _validate_filename(filename)
        path = self.memory_dir / filename
        path.write_text(content)
        self._update_entry(filename, content)

    def delete(self, filename: str) -> bool:
        """Delete a memory file and remove its index entry. Returns True if deleted."""
        filename = _validate_filename(filename)
        path = self.memory_dir / filename
        if not path.exists():
            return False
        path.unlink()
        self._remove_entry(filename)
        return True

    # ------------------------------------------------------------------
    # Prompt helper
    # ------------------------------------------------------------------

    def build_prompt_section(self) -> str:
        """Build the memory section for the system prompt.

        - Calls sync_index() to catch any files written via write_file().
        - If total content is small, includes full file content.
        - If total content is large, includes the index only with instructions
          to use recall_memory(filename) for full content.
        """
        self.sync_index()
        index = self._load_index()
        mem_dir = self.memory_dir.resolve()

        if not index:
            return (
                f"# Memory (directory: {mem_dir})\n"
                "No memory files yet. Use remember() to save knowledge, "
                "or write_file() to save a .md file directly."
            )

        # Load all file contents and check total size
        file_contents: list[tuple[str, str]] = []
        total_chars = 0
        for f in sorted(self.memory_dir.glob("*.md")):
            try:
                content = f.read_text().strip()
                total_chars += len(content)
                file_contents.append((f.name, content))
            except Exception:
                pass

        if total_chars <= FULL_LOAD_THRESHOLD:
            # Small enough — include full content
            lines = [f"# Memory (directory: {mem_dir})"]
            for name, content in file_contents:
                if content:
                    lines.append(f"## {name}\n{content}\n")
            return "\n".join(lines)
        else:
            # Too large — index only, agent must use recall_memory()
            lines = [
                f"# Memory (directory: {mem_dir})",
                f"You have {len(index)} memory file(s) ({total_chars:,} chars total). "
                "Files are too large to include in full — use recall_memory(filename) "
                "to read any file's full contents.",
                "",
                "Index:",
            ]
            for name, summary in sorted(index.items()):
                lines.append(f"- {name}: {summary}")
            return "\n".join(lines)
