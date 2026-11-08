"""Route PDF pages to text extraction or bounded page images for visual reading."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from . import converted
from .images import fit_image
from .types import Image, ReadError, ReadOptions, Reading

MAX_PAGE_IMAGES = 4
PAGE_DPI = 110
SCANNED_CHARS_PER_PAGE = 40
SCAN_SAMPLE_PAGES = 12
_PRINTER_MARKS = {"C", "M", "Y", "K", "CM", "MY", "CY", "CMY", "CMYK"}


def read(path: Path, options: ReadOptions) -> Reading:
    name = options.name_for(path)
    if options.as_images or _looks_scanned(path):
        # Scans are read by model vision; no OCR service is called here.
        return _read_pdf_pages(path, name, options.pages, as_images=True,
                               max_chars=options.max_chars, scanned=not options.as_images)
    if options.pages:
        return _read_pdf_pages(path, name, options.pages, as_images=False,
                               max_chars=options.max_chars)
    return converted.read(path, options)


def to_text(path: Path, max_chars: int) -> str | None:
    if _looks_scanned(path):
        return None
    return converted.to_text(path, max_chars)


def _parse_pages(pages: str | None, count: int) -> list[int]:
    """'3', '2-5', '1,4,7-9' (1-based) → 0-based page indexes within the document."""
    if not pages:
        return list(range(count))
    out: list[int] = []
    try:
        for part in pages.replace(" ", "").split(","):
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                start, end = int(a), int(b) if b else count
            else:
                start = end = int(part)
            out.extend(range(max(start, 1) - 1, min(end, count)))
    except ValueError as exc:
        raise ReadError(f"bad pages {pages!r}; use e.g. '3', '2-5' or '1,4,7-9'") from exc
    if not out:
        raise ReadError(f"pages {pages!r} are outside the document (it has {count} pages)")
    return list(dict.fromkeys(out))


def _looks_scanned(path: Path) -> bool:
    """Too little real text per page: a scan, or a catalogue whose text layer
    holds only printer marks ("C M Y CM MY CY CMY K"). Samples the first pages."""
    try:
        import fitz

        with fitz.open(path) as doc:
            sample = [cast(str, doc[i].get_text("text"))
                      for i in range(min(doc.page_count, SCAN_SAMPLE_PAGES))]
    except Exception:  # noqa: BLE001
        return False
    if not sample:
        return False
    real = sum(len(w) for text in sample for w in text.split()
               if w not in _PRINTER_MARKS and any(ch.isalnum() for ch in w))
    return real < SCANNED_CHARS_PER_PAGE * len(sample)


def _read_pdf_pages(path: Path, name: str, pages: str | None, *, as_images: bool,
                    max_chars: int, scanned: bool = False) -> Reading:
    import fitz

    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001
        raise ReadError(f"could not open {name} as a PDF: {exc}") from exc
    with doc:
        wanted = _parse_pages(pages, doc.page_count)
        if as_images:
            shown = wanted[:MAX_PAGE_IMAGES]
            images = []
            for i in shown:
                pix = doc[i].get_pixmap(dpi=PAGE_DPI)
                images.append(fit_image(Image("image/png", pix.tobytes("png"), f"page {i + 1}")))
            head = (f"{name} has no text layer (a scan); " if scanned else f"{name}: ")
            text = (head + f"pages {_ranges(shown)} of {doc.page_count} shown as images.")
            if len(wanted) > len(shown):
                rest = wanted[len(shown):]
                text += (f" Call read_file with pages='{_ranges(rest[:MAX_PAGE_IMAGES])}'"
                         f"{', as_images=true' if not scanned else ''} for the next ones.")
            return Reading(text, images)
        parts, used = [], 0
        for n, i in enumerate(wanted):
            body = cast(str, doc[i].get_text("text")).strip()
            block = f"--- page {i + 1} of {doc.page_count} ---\n{body}\n"
            if parts and used + len(block) > max_chars:
                rest = wanted[n:]
                parts.append(f"[{name}: output limit reached. Call read_file with "
                             f"pages='{_ranges(rest)}' to read on.]")
                break
            parts.append(block[:max_chars])
            used += len(block)
        return Reading("\n".join(parts))


def _ranges(indexes: list[int]) -> str:
    """0-based indexes → '1-3,7'."""
    out: list[str] = []
    start: int | None = None
    prev = 0
    for i in indexes:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            out.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
            start = prev = i
    if start is not None:
        out.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
    return ",".join(out)

