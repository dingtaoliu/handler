# Shared document reader

`handler.runtime.documents` is the public API used by the native `read_file` tool,
Codex's `handler-read` CLI, attachment downloads, intake previews and bounded
helper source loading. Start in `src/handler/runtime/documents/router.py`.

## Dispatch

`read(path, **options)` validates that the file exists, creates immutable
`ReadOptions`, classifies the file, and calls `READERS[file_type].read(path, options)`.
The native tool continues to enforce workspace confinement before calling it.

| Reader | Responsibility |
| --- | --- |
| `text.py` | Plain/source text decoding; existing JSON/JSONL/XML conversion |
| `converted.py` | Word, PowerPoint, HTML, EPUB, Outlook messages, notebooks, feeds |
| `spreadsheets.py` | Excel/CSV/TSV, content detection, sheets and A1-range tables |
| `pdf.py` | Text extraction, page selection, scan detection and visual-page strategy |
| `images.py` | Vision input, format conversion, orientation and resizing |
| `archives.py` | Existing ZIP member conversion through MarkItDown |

The router checks unsupported legacy `.doc`/`.ppt` first, then image signatures,
then format extensions. Spreadsheet readers detect whether an export is an actual
workbook, HTML or delimited text. Unknown extensions use the text reader, which
rejects binary/decoding garbage.

`to_text(path, max_chars=...)` is best effort and has a narrower supported-extension
policy than interactive reads. It dispatches by extension, preserving historical
preview behavior (including MarkItDown previews for plain text/Markdown), returns
`None` for scans, images, unsupported inputs or conversion failures, and never
adds paging instructions to the preview.

## Shared behavior

The native OpenAI harness uses a separate PDF transport adapter:
`harness/pdf_files.py` calls `pdf.prepare_input`, uploads the original PDF with
purpose `user_data`, and returns `ToolOutputFileContent(file_id=...)`. It includes
every page by default. A `pages` selection copies those pages into another PDF
without rasterizing them, with source-page provenance in the accompanying text.
The legacy `as_images` argument remains accepted but never converts native PDF
outputs into inline images. OpenAI handles the PDF's text and visual content.

Uploads are reused by content and API credential/project scope. References live
in `openai-pdf-files.json` in the thread's isolated, durably synced harness home,
outside the agent-writable workspace. Files remain available for later conversation
turns; they are not deleted at run completion. Upload failures return a tool error
without an inline-image fallback. PDFs at or above 50 MB require page selection.
This changes transport, not the model's token billing for reading PDF pages.
Existing inline-image history is not rewritten by this change.

- `types.py`: `ReadOptions`, `Reading`, `Image`, `ReadError` and text limits.
- `conversion.py`: local MarkItDown conversion and conversion-error messages.
- `cache.py`: 16-entry conversion cache keyed by path, size and modification time.
- `paging.py`: text slices and precise continuation instructions.
- `validation.py`: binary and decoding-garbage rejection.
- `cli.py` / `__main__.py`: installed `handler-read` and `python -m` entry points.

Reader modules implement `read(path, options)` and `to_text(path, max_chars)`.
To support another format, put its logic in the appropriate reader and register
its extension/family in the router; use the same result/options types.

Output limits remain 20,000 text characters per read, 500 characters per spreadsheet
cell, four PDF page images per CLI call, and 2048-pixel / 4-MB vision images. Read
continuation messages, cached formula values, sheet/range behavior and supported
formats are preserved. Large documents can still be converted locally in full
before paging the result; this refactor changes organization rather than that
resource policy. Scans use model vision; no OCR service or extra model call was
introduced.

## Validation

The existing 25 document/tool/CLI tests and 10 added routing/compatibility cases
pass. The full runtime suite passed **1,208 tests, 2 skipped**; the new package
also passes Pyright with zero errors/warnings. A one-off comparison against the pre-refactor module checked 86 cases over
20 files: text, structured documents, HTML, real/mislabeled spreadsheets, Word,
PDF text/pages/scans, images, archives, errors, previews and pagination. Returned
text, image bytes and errors were identical. Full runtime and deployment results
are recorded in HANDOFF.md.

Released as **v51** on `crontal-agent`, runtime `b6cc927` on `main`, after backup
**b029**; rollback **v50**. All three instance health/tools/reporting checks passed.
The deployed image passed native CSV, image and scanned-PDF reads, workspace
confinement, package/server imports and installed CLI execution.
