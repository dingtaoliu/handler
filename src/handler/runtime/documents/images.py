"""Return vision input, converting and resizing images to model limits."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from .. import images as image_types
from .types import Image, ReadError, ReadOptions, Reading

logger = logging.getLogger(__name__)
IMAGE_MAX_SIDE = 2048
IMAGE_MAX_BYTES = 4 * 1024 * 1024
_heif_registered = False


def read(path: Path, options: ReadOptions) -> Reading:
    name = options.name_for(path)
    data = path.read_bytes()
    mime = image_types.sniff(data[:16])
    # A vision type within the limits goes as is; anything else is re-encoded.
    image = fit_image(Image(mime or "", data, name), force=mime is None)
    return Reading(f"{name}: image ({_describe(data)}), shown below.", [image])


def to_text(path: Path, max_chars: int) -> None:
    # Image understanding happens through model vision, not a local OCR call.
    return None


def _register_heif() -> None:
    global _heif_registered
    if not _heif_registered:
        _heif_registered = True
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except Exception:  # noqa: BLE001 - optional; HEIC then reads as unsupported
            logger.info("pillow-heif unavailable; HEIC images can't be read")


def _open_image(data: bytes) -> Any:
    from PIL import Image as PILImage

    _register_heif()
    return PILImage.open(io.BytesIO(data))


def _max_side(data: bytes) -> int:
    try:
        with _open_image(data) as img:
            return max(img.size)
    except Exception:  # noqa: BLE001
        return 0


def _describe(data: bytes) -> str:
    try:
        with _open_image(data) as img:
            return f"{img.format or 'image'}, {img.size[0]}×{img.size[1]}, {len(data):,} bytes"
    except Exception:  # noqa: BLE001
        return f"{len(data):,} bytes"


def fit_image(image: Image, *, force: bool = False) -> Image:
    """Re-encode to PNG/JPEG within IMAGE_MAX_SIDE and IMAGE_MAX_BYTES if needed."""
    if (not force and len(image.data) <= IMAGE_MAX_BYTES
            and _max_side(image.data) <= IMAGE_MAX_SIDE):
        return image
    from PIL import ImageOps

    try:
        with _open_image(image.data) as img:
            img.seek(0)  # first frame of an animation
            frame = ImageOps.exif_transpose(img.copy())
    except Exception as exc:  # noqa: BLE001
        raise ReadError(f"{image.label or 'image'} could not be decoded as an image "
                        f"({type(exc).__name__})") from exc
    has_alpha = frame.mode in ("RGBA", "LA") or (
        frame.mode == "P" and "transparency" in frame.info)
    for side in (IMAGE_MAX_SIDE, IMAGE_MAX_SIDE // 2, IMAGE_MAX_SIDE // 4):
        if max(frame.size) > side:
            frame.thumbnail((side, side))
        buf = io.BytesIO()
        if has_alpha:
            frame.convert("RGBA").save(buf, "PNG", optimize=True)
            mime = "image/png"
        else:
            frame.convert("RGB").save(buf, "JPEG", quality=85)
            mime = "image/jpeg"
        if buf.tell() <= IMAGE_MAX_BYTES:
            return Image(mime, buf.getvalue(), image.label)
    raise ReadError(f"{image.label or 'image'} is too large to show even after downscaling")
