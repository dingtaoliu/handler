"""Memory: agent-controlled knowledge stored as topic files with a central index.

Files are stored as plain markdown (.md) in data/memory/. A central index.md
maps topic names to short descriptions and is always included in the system
prompt. Full topic content is loaded on demand via the memory tool.

Index format (index.md):
    - topic_name.md: Short description of what this topic contains
    - another_topic.md: Another description
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger("handler.memory")

_INDEX_FILE = "index.md"


def _validate_topic(name: str) -> str:
    """Sanitize and validate a topic name → filename.

    Strips path components, ensures .md extension, rejects path traversal
    and non-alphanumeric characters. Raises ValueError on invalid input.
    """
    name = Path(name).name  # strip any directory components
    if not name or ".." in name:
        raise ValueError(f"Invalid topic name: {name!r}")
    if not name.endswith(".md"):
        name += ".md"
    if not re.match(r"^[\w][\w\-\.]*\.md$", name) or len(name) > 120:
        raise ValueError(f"Invalid topic name: {name!r}")
    if name == _INDEX_FILE:
        raise ValueError("Cannot use 'index.md' as a topic name.")
    return name


class Memory:
    """Manages the agent-controlled memory directory (data/memory/*.md)."""

    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _index_path(self) -> Path:
        return self.memory_dir / _INDEX_FILE

    # ------------------------------------------------------------------
    # Index operations
    # ------------------------------------------------------------------

    def _load_index(self) -> dict[str, str]:
        """Parse index.md into {filename: description}."""
        if not self._index_path.exists():
            return {}
        index = {}
        for line in self._index_path.read_text().splitlines():
            line = line.strip()
            m = re.match(r"^-\s+(\S+\.md):\s*(.*)$", line)
            if m:
                index[m.group(1)] = m.group(2).strip()
        return index

    def _save_index(self, index: dict[str, str]) -> None:
        """Write index.md from {filename: description}."""
        lines = []
        for filename, description in sorted(index.items()):
            lines.append(f"- {filename}: {description}")
        self._index_path.write_text("\n".join(lines) + "\n" if lines else "")

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def list_topics(self) -> list[dict]:
        """Return metadata for all topics, sorted by name."""
        index = self._load_index()
        result = []
        for f in sorted(self.memory_dir.glob("*.md")):
            if f.name == _INDEX_FILE:
                continue
            result.append(
                {
                    "filename": f.name,
                    "description": index.get(f.name, ""),
                    "size": f.stat().st_size,
                }
            )
        return result

    @staticmethod
    def _derive_description(topic: str, content: str) -> str:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped.removeprefix("# ")[:120]
        return Path(topic).stem.replace("_", " ")[:120]

    def read(self, topic: str) -> str:
        """Read the full content of a topic file."""
        filename = _validate_topic(topic)
        path = self.memory_dir / filename
        if not path.exists():
            return ""
        return path.read_text()

    def write(self, topic: str, content: str) -> str:
        """Backward-compatible full rewrite that preserves the existing description."""
        filename = _validate_topic(topic)
        index = self._load_index()
        description = index.get(filename) or self._derive_description(filename, content)
        path = self.memory_dir / filename
        path.write_text(content)
        index[filename] = description
        self._save_index(index)
        logger.info(f"memory write: {filename}")
        return "written"

    def save(self, topic: str, content: str, description: str) -> str:
        """Upsert a topic: create if new, append if existing. Updates index.

        Returns a status message.
        """
        filename = _validate_topic(topic)
        path = self.memory_dir / filename
        index = self._load_index()

        if path.exists():
            existing = path.read_text()
            path.write_text(existing + "\n" + content)
            mode = "appended"
        else:
            path.write_text(content)
            mode = "created"

        index[filename] = description
        self._save_index(index)
        logger.info(f"memory save: {mode} {filename}")
        return mode

    def rewrite(
        self,
        topic: str,
        content: str,
        description: str,
        new_topic: str = "",
    ) -> str:
        """Full rewrite of a topic: replaces content, description, and optionally renames.

        Returns a status message.
        """
        filename = _validate_topic(topic)
        path = self.memory_dir / filename
        index = self._load_index()

        if new_topic and new_topic != topic:
            new_filename = _validate_topic(new_topic)
            new_path = self.memory_dir / new_filename

            # Write new file
            new_path.write_text(content)

            # Remove old file if it exists
            if path.exists():
                path.unlink()
            index.pop(filename, None)

            index[new_filename] = description
            self._save_index(index)
            logger.info(f"memory rewrite: renamed {filename} → {new_filename}")
            return f"rewritten and renamed to {new_filename}"
        else:
            path.write_text(content)
            index[filename] = description
            self._save_index(index)
            logger.info(f"memory rewrite: {filename}")
            return "rewritten"

    def delete(self, topic: str) -> bool:
        """Delete a topic file and its index entry. Returns True if deleted."""
        filename = _validate_topic(topic)
        path = self.memory_dir / filename
        if not path.exists():
            return False
        path.unlink()
        index = self._load_index()
        index.pop(filename, None)
        self._save_index(index)
        logger.info(f"memory delete: {filename}")
        return True

    # ------------------------------------------------------------------
    # Prompt helper
    # ------------------------------------------------------------------

    def build_prompt_section(self) -> str:
        """Build the memory section for the system prompt.

        Always includes the index only. Agent uses memory(action='read')
        to load full content on demand.
        """
        index = self._load_index()

        if not index:
            return (
                "# Memory\n"
                "No memory topics yet. Use memory(action='save') to store knowledge."
            )

        lines = [
            "# Memory",
            f"You have {len(index)} memory topic(s). "
            "Use memory(action='read', topic='...') to load full content.",
            "",
        ]
        for filename, description in sorted(index.items()):
            lines.append(f"- {filename}: {description}")
        return "\n".join(lines)
