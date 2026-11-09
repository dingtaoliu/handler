# CSV spreadsheet exports

The shared MCP gateway exposes `write_spreadsheet` to both native OpenAI and
Codex harnesses. `runtime/spreadsheets.py` validates/serializes the table;
`tools/spreadsheets.py` wraps it with run ownership and artifact persistence.

Inputs: workspace-relative `.csv` path, ordered `columns` (`key`, `heading`,
`type`), object `rows`, optional `overwrite=false`. Types: text, number, boolean,
ISO date; null/missing becomes blank. Limits: 50,000 rows, 100 columns, 20,000
characters per text cell, 10 MiB output. Unknown fields and invalid data fail
without overwriting an existing file. Staging plus atomic no-clobber publication
prevents partial files and accidental replacement. Symlink escapes are rejected.

CSV uses UTF-8 BOM, CRLF, standard quoting and invariant numeric formatting.
Formula-looking text is prefixed with an apostrophe; the compact receipt reports
how many cells were literalized. Negative values in numeric columns remain numeric.
No formulas are evaluated. CSV cannot preserve styles, tabs, merged cells or
portable recalculation. Identifiers retain their CSV text, but spreadsheet apps
may auto-convert them; import identifier columns as text when necessary.

For an owned run, the tool persists immutable bytes and metadata in the instance's
existing FileStore (`artifacts/<random-id>/data` and `meta.json`), rather than the
ephemeral incoming-upload directory. `transcript_message` attaches a normal file
block to a successful tool result. Both existing UIs already render these blocks
as authenticated download buttons. `/api/uploads/<id>` falls back to durable
artifacts, checks owner/admin authorization and returns the existing owner header
that Crontal's proxy also checks. A fresh process can read the artifact without
the original workspace or upload cache. No frontend change or sandbox link is needed.

The file is a Handler artifact, **not automatically a Crontal Files record**.
Use the existing Crontal upload tool when the task calls for that additional
business record. Unowned runs get a workspace export with an explicit delivery
note; they cannot create an artifact falsely attributed to a user. Storage failure
leaves the valid workspace file but returns a tool error and does not emit an
attachment. Artifacts are immutable snapshots: overwriting a workspace CSV creates
a new artifact and does not change downloads attached to earlier messages.

Focused tests cover serialization, Unicode, embedded punctuation/newlines, typed
values, literalization, invalid-data preservation, path confinement, size limits,
no-clobber writes, fresh-store retrieval, native/CLI MCP wrappers and authenticated
download isolation. Excel/Google Sheets interactive import and a browser click
have not been exercised in this environment; do not claim they were.
