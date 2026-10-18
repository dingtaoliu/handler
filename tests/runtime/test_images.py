from __future__ import annotations

import io

from PIL import Image

from handler.runtime.images import downscale, extension, sniff, sniff_file


def _encode(fmt: str, mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (3, 3)).save(buf, fmt)
    return buf.getvalue()


def test_sniff_recognises_vision_types_by_bytes():
    assert sniff(_encode("PNG")) == "image/png"
    assert sniff(_encode("JPEG")) == "image/jpeg"
    assert sniff(_encode("WEBP")) == "image/webp"
    assert sniff(_encode("GIF", "P")) == "image/gif"
    assert sniff(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None
    assert sniff(_encode("BMP")) is None
    assert sniff(b"") is None
    assert extension("image/webp") == ".webp" and extension("text/plain") == ""


def test_sniff_file_missing_is_none(tmp_path):
    assert sniff_file(tmp_path / "nope.png") is None


def test_downscale_keeps_transparency_as_png(tmp_path):
    source = tmp_path / "a.png"
    Image.new("RGBA", (3000, 1000), (0, 0, 0, 0)).save(source)
    out = downscale(source, tmp_path / "a.small")
    assert out == tmp_path / "a.small.png"
    with Image.open(out) as img:
        assert img.size == (2048, 683) and img.mode == "RGBA"


def test_downscale_of_a_non_image_is_none(tmp_path):
    source = tmp_path / "x.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n broken")
    assert downscale(source, tmp_path / "x.small") is None
