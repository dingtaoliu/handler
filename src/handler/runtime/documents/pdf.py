"""Route PDF pages to text extraction or bounded page images for visual reading.

All PDF text comes from PyMuPDF, page by page, as `--- page N of M ---` blocks.
MarkItDown (pdfminer) is never used for PDFs: its conversion needed 244 MB for
a 2-page PDF and gigabytes for long ones, over the reader's RSS cap.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .conversion import convert_document
from .images import fit_image
from .paging import page_text
from .types import Image, ReadError, ReadOptions, Reading
from .validation import check_text

MAX_PAGE_IMAGES = 4
PAGE_DPI = 110
SCANNED_CHARS_PER_PAGE = 40
SCAN_SAMPLE_PAGES = 12
_PRINTER_MARKS = {"C", "M", "Y", "K", "CM", "MY", "CY", "CMY", "CMYK"}


MAX_UPLOAD_BYTES = 50_000_000


@dataclass(frozen=True)
class PDFUpload:
    path: Path  # the file to upload: the original, or a copy of the selected pages
    description: str
    size: int
    copy: bool  # `path` is a temporary copy the caller removes


def prepare_upload(path: Path, *, display: str, pages: str | None = None,
                   scratch: Path) -> PDFUpload:
    """Check the PDF, and copy selected pages to `scratch` without rasterizing them.

    Runs in a reader process: only paths cross the pipe, never the PDF's bytes.
    """
    import fitz

    target, copy = Path(path), False
    try:
        with fitz.open(path, filetype="pdf") as source:
            if source.needs_pass:
                raise ReadError(f"{display} is password-protected")
            if not source.page_count:
                raise ReadError(f"{display} has no pages")
            wanted = _parse_pages(pages, source.page_count)
            if pages:
                with fitz.open() as selected:
                    for index in wanted:
                        selected.insert_pdf(source, from_page=index, to_page=index)
                    data = selected.tobytes(garbage=4, deflate=True, no_new_id=True)
                target, copy = Path(scratch) / f"{uuid.uuid4().hex}.pdf", True
                target.write_bytes(data)
                del data
            description = (f"{display}: original pages {_ranges(wanted)} of "
                           f"{source.page_count}, supplied as a PDF. "
                           "Read all supplied pages, including tables and scanned content.")
    except ReadError:
        raise
    except Exception as exc:
        raise ReadError(f"could not open {display} as a PDF: {exc}") from exc
    size = target.stat().st_size
    if size >= MAX_UPLOAD_BYTES:
        if copy:
            target.unlink(missing_ok=True)
        raise ReadError(f"{display} exceeds the 50 MB PDF input limit; select fewer pages")
    return PDFUpload(target, description, size, copy)


def read(path: Path, options: ReadOptions) -> Reading:
    name = options.name_for(path)
    if options.as_images or _looks_scanned(path):
        # Scans are read by model vision; no OCR service is called here.
        return _read_pdf_pages(path, name, options.pages, as_images=True,
                               max_chars=options.max_chars, scanned=not options.as_images)
    if options.pages:
        return _read_pdf_pages(path, name, options.pages, as_images=False,
                               max_chars=options.max_chars)
    # The whole document's page text is extracted once and cached (cache.py: per
    # process, or the pool's shared directory), so offset continuations page
    # through it without re-extracting, whichever reader serves them.
    text = convert_document(path, name, pdf_page_text)
    return page_text(text, options.offset, options.max_chars, name)


def to_text(path: Path, max_chars: int) -> str | None:
    if _looks_scanned(path):
        return None
    # Previews and bounded tasks stop extracting once they have max_chars.
    return check_text(pdf_page_text(path, limit=max_chars), path.name)[:max_chars]


def pdf_page_text(path: Path, limit: int | None = None) -> str:
    """The document's text as page blocks, extracted one page at a time.

    With `limit`, stops after the page that reaches `limit` characters.
    """
    import fitz

    parts: list[str] = []
    used = 0
    with fitz.open(path, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ReadError(f"{Path(path).name} is password-protected")
        for i in range(doc.page_count):
            block = _page_block(doc, i)
            parts.append(block)
            used += len(block) + 1
            if limit is not None and used >= limit:
                break
    return "\n".join(parts)


def _page_block(doc, index: int) -> str:  # noqa: ANN001 - fitz.Document
    body = cast(str, doc[index].get_text("text")).strip()
    return f"--- page {index + 1} of {doc.page_count} ---\n{body}\n"


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
            block = _page_block(doc, i)
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
