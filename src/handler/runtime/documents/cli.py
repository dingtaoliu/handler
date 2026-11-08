"""The handler-read command used by the Codex harness."""

from __future__ import annotations

from pathlib import Path

from .. import images as image_types
from .router import IMAGE_SUFFIXES, read
from .types import MAX_READ_CHARS, ReadError

def main(argv: list[str] | None = None) -> int:
    """handler-read FILE [--sheet S] [--cells R] [--pages P] [--offset N].

    Prints the same text the native harness's read_file returns. Page images
    (a scanned PDF, or --pages with --as-images) are saved under --images-dir
    and their paths printed, for Codex's view_image tool.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="handler-read", description=main.__doc__)
    parser.add_argument("path")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--sheet")
    parser.add_argument("--cells")
    parser.add_argument("--pages")
    parser.add_argument("--as-images", action="store_true", help="render PDF pages as images")
    parser.add_argument("--images-dir", default=".tmp/handler-read")
    parser.add_argument("--max-chars", type=int, default=MAX_READ_CHARS)
    args = parser.parse_args(argv)
    source = Path(args.path)
    if image_types.sniff_file(source) or source.suffix.lower() in IMAGE_SUFFIXES:
        print(f"{args.path} is an image: open it with view_image.")
        return 0
    try:
        reading = read(source, display=args.path, offset=args.offset, sheet=args.sheet,
                       cells=args.cells, pages=args.pages, as_images=args.as_images,
                       max_chars=args.max_chars)
    except ReadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(reading.text)
    if reading.images:
        out_dir = Path(args.images_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for image in reading.images:
            ext = image_types.extension(image.mime) or ".png"
            target = out_dir / f"{source.stem}-{image.label.replace(' ', '')}{ext}"
            target.write_bytes(image.data)
            print(f"image: {target} (open it with view_image)")
    return 0
