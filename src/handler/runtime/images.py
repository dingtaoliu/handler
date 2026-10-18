"""Chat images the model can see: type sniffing, limits and downscaling.

An image attached to a chat message reaches the harness twice: as a vision
input (an `input_image` part for openai_agents, `-i` for codex) and as a file
under the workspace's attachments/ so tools can still open it later.

Only PNG, JPEG, WebP and GIF are sent as vision input, detected from the bytes
(never from the client's claimed type or the file name). An image over
MAX_IMAGE_BYTES is downscaled for the model; at most MAX_IMAGES_PER_MESSAGE
images per message are sent as vision input. Anything else stays a file.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

VISION_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 6
DOWNSCALE_MAX_SIDE = 2048

_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def sniff(head: bytes) -> str | None:
    """The vision image type of these leading bytes, or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_file(path: Path) -> str | None:
    try:
        with Path(path).open("rb") as f:
            return sniff(f.read(16))
    except OSError:
        return None


def extension(mime: str) -> str:
    return _EXT.get(mime, "")


def downscale(source: Path, target_stem: Path, *, max_bytes: int = MAX_IMAGE_BYTES,
              max_side: int = DOWNSCALE_MAX_SIDE) -> Path | None:
    """Write a smaller copy of `source` next to `target_stem` (extension added).

    Returns the new file, or None when Pillow is missing, the image can't be
    decoded, or it is still over max_bytes after shrinking.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with pdfplumber
        logger.warning("Pillow unavailable; cannot downscale %s", source.name)
        return None
    try:
        with Image.open(source) as img:
            img.seek(0)  # first frame of an animation
            frame = img.copy()
        frame.thumbnail((max_side, max_side))
        has_alpha = frame.mode in ("RGBA", "LA") or (
            frame.mode == "P" and "transparency" in frame.info)
        for side in (max_side, max_side // 2, max_side // 4):
            if max(frame.size) > side:
                frame.thumbnail((side, side))
            buf = io.BytesIO()
            if has_alpha:
                frame.convert("RGBA").save(buf, "PNG", optimize=True)
                suffix = ".png"
            else:
                frame.convert("RGB").save(buf, "JPEG", quality=85)
                suffix = ".jpg"
            if buf.tell() <= max_bytes:
                out = target_stem.with_name(target_stem.name + suffix)
                out.write_bytes(buf.getvalue())
                return out
    except Exception:  # noqa: BLE001 - any decode failure: not a usable image
        logger.warning("could not downscale %s", source.name, exc_info=True)
    return None
