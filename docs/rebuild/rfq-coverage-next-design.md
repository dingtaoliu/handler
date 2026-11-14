# RFQ extraction coverage: preventive follow-up design

This is a concrete follow-up to the read-only RFQ562 investigation, not a repair
or a claim that the current extractor is complete. The catalogue/unit/preflight
release solves structured mapping loss; the independent one-shot extraction
pipeline still needs source coverage enforcement.

## Why a model swap alone is insufficient

The reproduced XLS is about 27k characters and all sections survive conversion.
The historical cheap extraction call returned/persisted only 16 of 102 product
rows, with an omission warning; its raw response/finish reason was not retained.
A stronger model may improve recall but cannot establish complete coverage by
itself. The current 100-item persistence cap also cannot represent 102 lines.
Do not downgrade the main Handler model to address this separate extractor.

## Proposed bounded spreadsheet contract

1. Inventory source rows deterministically before inference. Preserve source
   file SHA-256, sheet index/name, physical row number and row hash; repeated
   item codes and restarted sequence numbers are not deduplication keys. Retain
   section/header context, explicit quantities and source units.
2. For CSV/XLS/XLSX, send bounded row batches with stable source IDs. Require one
   result per supplied row: item, context/non-item with a reason, or unresolved.
   A row with an explicit positive ordered quantity must not be silently
   classified away. Missing, repeated or fabricated source IDs fail reconciliation.
3. Meter every request and record source counts/hash, provider request ID, finish
   reason, parsed/accepted/rejected counts and batch boundaries. Keep full source
   documents in existing authorized file storage, not duplicated in log payloads.
4. Aggregate validated line items only after reconciling every batch. Preserve
   unknown fields as source/quality text and review flags. No silent 100-item cap;
   bounded total-work limits must produce a partial outcome with explicit counts.
5. Persist coverage plus source-to-created-line links transactionally. Numerical
   count equality is necessary but does not establish correct source coverage or
   steel standards. Do not declare verification merely because 102 IDs exist.

## Review and lifecycle

Known incomplete coverage must block confirmation/quoting. Preserve extraction
warnings after confirmation, record the actual confirmation actor/time and keep
review exceptions separate from machine evidence. Unknown historical coverage
stays unknown; do not backfill success or attribute a human/agent without evidence.
A retry must reconcile/replace the intended source lines without duplicates and
must not overwrite manual edits or an existing dependent quote silently.

For non-spreadsheet documents, track page/text boundaries and truncation/OCR limits
explicitly. The current received-RFQ OCR path caps scans at 12 pages; it is separate
from Handler's fixed native-PDF read_file transport. A 15-page source cannot be
called complete after 12 pages. Page-based extraction needs equivalent coverage
receipts and a bounded continuation path, not silent clipping.

## Acceptance before a runtime change

Use synthetic fixtures: 102 rows in three sections with restarted numbering;
blank/zero-quantity note rows; multi-sheet XLSX; repeated item codes; ambiguous
units; invalid product mappings; a model omitting/duplicating/fabricating row IDs;
a failed middle batch; an edited source/retry; a 15-page scan; a pending quote;
and explicit human confirmation. Verify every source ID, exact quantity/unit,
source references, non-duplication, preserved warnings, quota/spend aggregation
and confirmation gates. Compare bounded extraction cost with complete quality,
not the old cheap partial output. No customer-data model replay is required.

## Historical repair boundary

A protected local inventory now reconciles all 102 source rows against the existing
16 and identifies 86 missing rows. Quote232 is an unpriced, unsaved, unlocked DRAFT.
Choose and review in-place repair versus a replacement record before making any
business writes; preserve original lines/source and dependent-quote provenance.
The row inventory is preparation, not a reviewed product/price payload or repair
authorization. No historical RFQ/quote was modified by this investigation.

## November 12 implementation

Crontal **v124** (`dab8c97`, image `4bc02f43ac46`, 741,381,019 bytes) implements
spreadsheet inventories/batches, strict row reconciliation, compact ordinary
responses and owned full audits, transactional line/evidence persistence, known
partial confirmation/quote gates, warning retention and trusted confirmation
attribution. Backup **b039**, migration **z1coverage20261112**, rollback Crontal
**v123**; Handler stays **v58**. **684 backend tests / 22 subtests**, frontend
build/i18n and disposable PostgreSQL migration checks passed. Live ownership,
health, new frontend, both user proxies and historical-record preservation passed.

Full implementation and limits: sibling Crontal
`docs/rfq-source-coverage-work.md`. The document path still has its existing
12-page OCR / 100-item limits, now explicitly partial and blocked; page-batched
continuation is not implemented. A known partial import needs a new complete
corrected source, not an override. Historical RFQ562/quote232 remain unchanged.
The protected local XLS inventory also includes 243 zero-quantity rows (349 total
non-empty rows), so retaining every row produces 18 proposed batches; do not
claim the six-call synthetic 102-positive-row case is the full historical cost.
