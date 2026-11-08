"""Read Excel and delimited tables by sheet and A1 range, including mislabeled exports."""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, cast

from .cache import cached_conversion
from .conversion import convert_document, markdown_html
from .paging import page_text
from .text import plain_text
from .types import ReadError, ReadOptions, Reading
from .validation import binary_error

MAX_CELL_CHARS = 500


def read(path: Path, options: ReadOptions) -> Reading:
    name = options.name_for(path)
    if _spreadsheet_kind(path) == "html":
        text = convert_document(path, name, markdown_html)
        return page_text(text, options.offset, options.max_chars, name)
    return _render_workbook(_open_workbook(path), sheet=options.sheet,
                            cells=options.cells, budget=options.max_chars, name=name)


def to_text(path: Path, max_chars: int) -> str:
    if _spreadsheet_kind(path) == "html":
        return cached_conversion(path, markdown_html)[:max_chars]
    return _render_workbook(_open_workbook(path), budget=max_chars, name=path.name).text


@dataclass
class _Sheet:
    name: str
    max_row: int | None
    max_col: int | None
    # rows(min_row, max_row, min_col, max_col) → (row number, values)
    rows: Callable[[int, int | None, int, int | None], Iterator[tuple[int, list[Any]]]]


@dataclass
class _Workbook:
    sheets: list[_Sheet]
    close: Callable[[], None] = lambda: None


def _spreadsheet_kind(path: Path) -> str:
    """From the bytes, since exports often lie: xlsx, xls, html, binary or text.

    Many ERP and bank exports named .xls are really HTML tables or CSV.
    """
    with path.open("rb") as f:
        head = f.read(2048)
    if head.startswith(b"PK\x03\x04"):
        return "xlsx"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "xls"
    lower = head.lower()
    probe = lower.lstrip(b"\xef\xbb\xbf \t\r\n")
    if probe.startswith(b"<") and (b"<table" in lower or b"<html" in probe[:300]
                                   or b"<!doctype" in probe[:300]):
        return "html"
    if b"\x00" in head:
        return "binary"
    return "text"


def _open_workbook(path: Path) -> _Workbook:
    kind = _spreadsheet_kind(path)
    if kind == "xlsx":
        return _xlsx_workbook(path)
    if kind == "xls":
        return _xls_workbook(path)
    if kind == "binary":
        raise binary_error(path.name, path.stat().st_size)
    return _csv_workbook(path, "\t" if path.suffix.lower() == ".tsv" else None)


def _xlsx_workbook(path: Path) -> _Workbook:
    import openpyxl

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ReadError(f"could not open {path.name} as a workbook ({exc}); nothing was "
                        "shown. It may be corrupt or password-protected.") from exc
    sheets = []
    for ws in wb.worksheets:
        def rows(min_row, max_row, min_col, max_col, ws=ws):
            for i, row in enumerate(ws.iter_rows(min_row=min_row, max_row=max_row,
                                                 min_col=min_col, max_col=max_col,
                                                 values_only=True), start=min_row):
                yield i, list(row)
        sheets.append(_Sheet(ws.title, ws.max_row, ws.max_column, rows))
    return _Workbook(sheets, wb.close)


def _xls_workbook(path: Path) -> _Workbook:
    import xlrd

    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as exc:  # noqa: BLE001
        raise ReadError(f"could not open {path.name} as an .xls workbook ({exc}); nothing "
                        "was shown. It may be corrupt or password-protected: ask for it "
                        "as .xlsx or PDF.") from exc
    sheets = []
    for index, title in enumerate(book.sheet_names()):
        sh = book.sheet_by_index(index)

        def rows(min_row, max_row, min_col, max_col, sh=sh):
            last_row = min(sh.nrows, max_row or sh.nrows)
            for r in range(min_row - 1, last_row):
                last_col = min(sh.ncols, max_col or sh.ncols)
                values = []
                for c in range(min_col - 1, last_col):
                    cell = sh.cell(r, c)
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            values.append(xlrd.xldate.xldate_as_datetime(cast(float, cell.value),
                                                                         book.datemode))
                            continue
                        except Exception:  # noqa: BLE001
                            pass
                    values.append(None if cell.ctype in (xlrd.XL_CELL_EMPTY,
                                                         xlrd.XL_CELL_BLANK) else cell.value)
                yield r + 1, values
        sheets.append(_Sheet(title, sh.nrows, sh.ncols, rows))
    return _Workbook(sheets, book.release_resources)


def _csv_workbook(path: Path, delimiter: str | None) -> _Workbook:
    text = plain_text(path, path.name)
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    table = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    width = max((len(r) for r in table), default=0)

    def rows(min_row, max_row, min_col, max_col):
        for r in range(min_row - 1, min(len(table), max_row or len(table))):
            yield r + 1, table[r][min_col - 1:max_col]
    return _Workbook([_Sheet(path.stem, len(table), width, rows)])


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    text = str(value)
    if len(text) > MAX_CELL_CHARS:
        text = text[:MAX_CELL_CHARS] + "…"
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _parse_cells(cells: str | None) -> tuple[int, int | None, int, int | None]:
    """'B2:F50', '100:200', 'C:E' or 'A10' → (min_row, max_row, min_col, max_col)."""
    if not cells:
        return 1, None, 1, None
    from openpyxl.utils import range_boundaries

    spec = cells.strip().upper().replace("$", "")
    if ":" not in spec:
        spec = f"{spec}:{spec}"
    try:
        min_col, min_row, max_col, max_row = range_boundaries(spec)
    except ValueError as exc:
        raise ReadError(f"bad cells range {cells!r}; use A1 notation like 'A1:H200', "
                        "'100:300' (rows) or 'C:F' (columns)") from exc
    return min_row or 1, max_row, min_col or 1, max_col


def _letter(col: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(col)


def _render_workbook(wb: _Workbook, *, sheet: str | None = None, cells: str | None = None,
                     budget: int | None, name: str = "") -> Reading:
    try:
        return _render(wb, sheet, cells, budget, name)
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass


def _dims(s: _Sheet) -> str:
    if s.max_row is None or s.max_col is None:
        return "size unknown"
    if not s.max_row or not s.max_col:
        return "empty"
    return f"A1:{_letter(s.max_col)}{s.max_row}"


def _render(wb: _Workbook, sheet: str | None, cells: str | None, budget: int | None,
            name: str) -> Reading:
    sheets = wb.sheets
    if sheet is not None:
        match = [s for s in sheets if s.name == sheet] or [
            s for s in sheets if s.name.strip().lower() == sheet.strip().lower()]
        if not match:
            raise ReadError(f"no sheet named {sheet!r}; sheets: "
                            + ", ".join(repr(s.name) for s in sheets))
        selected = match[:1]
    else:
        selected = sheets
    min_row, max_row, min_col, max_col = _parse_cells(cells)
    out: list[str] = []
    if len(sheets) > 1 or sheet is None:
        out.append(f"Workbook with {len(sheets)} sheet(s): "
                   + "; ".join(f"{s.name!r} ({_dims(s)})" for s in sheets))
    used = sum(len(x) + 1 for x in out)
    for index, s in enumerate(selected):
        heading = f"## Sheet: {s.name} ({_dims(s)})"
        rows: list[tuple[int, list[str]]] = []
        size = len(heading) + 2
        stopped_at: int | None = None
        for number, values in s.rows(min_row, max_row, min_col, max_col):
            texts = [_cell(v) for v in values]
            while texts and texts[-1] == "":
                texts.pop()
            if not texts:
                continue
            line = 8 + len(str(number)) + sum(len(t) + 3 for t in texts)
            if budget is not None and rows and used + size + line > budget:
                stopped_at = number
                break
            rows.append((number, texts))
            size += line
        out.append(heading)
        if rows:
            width = max(len(t) for _, t in rows)
            columns = [_letter(min_col + i) for i in range(width)]
            out.append("| row | " + " | ".join(columns) + " |")
            out.append("|---|" + "---|" * width)
            for number, texts in rows:
                out.append(f"| {number} | " + " | ".join(texts + [""] * (width - len(texts)))
                           + " |")
        else:
            out.append("(no values in this range)")
        used += size
        if stopped_at is not None:
            last_col = _letter(max_col) if max_col else (_letter(s.max_col) if s.max_col else "")
            rng = f"{_letter(min_col)}{stopped_at}:{last_col}{max_row or s.max_row or ''}"
            rest = [x.name for x in selected[index + 1:]]
            note = (f"[{name}: output limit reached in sheet {s.name!r} before row {stopped_at}"
                    f"{f' of {s.max_row}' if s.max_row else ''}. Call read_file with "
                    f"sheet={s.name!r} and cells={rng!r} to read on (a narrower column "
                    "range fits more rows)")
            if rest:
                note += "; not shown yet: " + ", ".join(repr(r) for r in rest)
            out.append(note + ".]")
            break
    return Reading("\n".join(out).rstrip() + "\n")

