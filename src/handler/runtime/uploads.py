"""User file uploads: `<root>/<user>/<file_id>/<sanitized filename>` plus a meta.json."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from .images import sniff

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES_PER_REQUEST = 10
_META = "meta.json"
_FILE_ID = re.compile(r"[0-9a-f]{32}")


class UploadTooLarge(Exception):
    pass


@dataclass
class UploadedFile:
    id: str
    user_id: str
    filename: str
    size: int
    content_type: str

    def public(self) -> dict:
        return {"id": self.id, "filename": self.filename, "size": self.size,
                "content_type": self.content_type}


def sanitize_filename(name: str | None) -> str:
    """A bare, filesystem-safe file name (never a path, never hidden or empty)."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .")
    return (base or "file")[:120]


def _safe_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]", "_", value).lstrip(".") or "_"


class UploadStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _dir(self, user_id: str, file_id: str) -> Path:
        return self.root / _safe_part(user_id) / file_id

    def new_id(self) -> str:
        return secrets.token_hex(16)

    def save(self, user_id: str, filename: str | None, content_type: str | None,
             chunks, *, max_bytes: int = MAX_FILE_BYTES) -> UploadedFile:
        """Write `chunks` (an iterable of bytes); raises UploadTooLarge past max_bytes."""
        file_id = self.new_id()
        name = sanitize_filename(filename)
        directory = self._dir(user_id, file_id)
        directory.mkdir(parents=True)
        path = directory / name
        size = 0
        head = b""
        try:
            with path.open("wb") as out:
                for chunk in chunks:
                    if len(head) < 16:
                        head += bytes(chunk[: 16 - len(head)])
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadTooLarge(f"{name} exceeds {max_bytes} bytes")
                    out.write(chunk)
            # A recognised image gets its real type (pasted files often claim none).
            meta = UploadedFile(file_id, user_id, name, size,
                                sniff(head) or content_type or "application/octet-stream")
            (directory / _META).write_text(json.dumps(asdict(meta)))
        except BaseException:
            self._remove(directory)
            raise
        return meta

    def get(self, file_id: str) -> UploadedFile | None:
        if not _FILE_ID.fullmatch(file_id or ""):
            return None
        for meta_path in self.root.glob(f"*/{file_id}/{_META}"):
            try:
                return UploadedFile(**json.loads(meta_path.read_text()))
            except (OSError, ValueError, TypeError):
                return None
        return None

    def path(self, meta: UploadedFile) -> Path | None:
        """The stored file, or None if it is missing or would escape the root."""
        path = self._dir(meta.user_id, meta.id) / sanitize_filename(meta.filename)
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(self.root.resolve()) or not resolved.is_file():
                return None
        except OSError:
            return None
        return resolved

    def list(self, user_id: str | None = None) -> list[UploadedFile]:
        pattern = f"{_safe_part(user_id)}/*/{_META}" if user_id else f"*/*/{_META}"
        found: list[tuple[float, UploadedFile]] = []
        for meta_path in self.root.glob(pattern):
            try:
                found.append((meta_path.stat().st_mtime,
                              UploadedFile(**json.loads(meta_path.read_text()))))
            except (OSError, ValueError, TypeError):
                continue
        return [m for _, m in sorted(found, key=lambda t: t[0], reverse=True)]

    def delete(self, meta: UploadedFile) -> None:
        self._remove(self._dir(meta.user_id, meta.id))

    @staticmethod
    def _remove(directory: Path) -> None:
        if directory.is_dir():
            for child in directory.iterdir():
                child.unlink(missing_ok=True)
            directory.rmdir()
