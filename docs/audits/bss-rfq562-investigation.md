# BSS-001: incomplete Stirlings RFQ 562

Investigation performed read-only following the owner's instruction to start
the improvement items sequentially. No RFQ, quote or source file was changed.

## Verified findings

Original Crontal file 995 was downloaded using the BSS AI identity. SHA-256:
`cb4e5a34ddddcbf60e89cc57b759f7db0178384864c27b3b6e8c0d8bcbc144d6`.
The XLS workbook's sheet `00000652` has 379 physical rows. Excluding blank and
zero-quantity Note Line rows leaves **102 product rows**:

| Section | Product rows | Source row positions |
| --- | ---: | --- |
| Fremantle FCL Bundles | 63 | 3–69, excluding blanks |
| Brisbane LCL Boxed | 20 | 72–93, excluding blanks |
| Melbourne LCL Boxed | 19 | 96–114 |

RFQ 562 contains exactly 16 listings, IDs 562–577. They correspond to original
sheet rows 3–15 and 17–19: the first 13 duplex and three super-duplex lines.
Missing: **47 remaining Fremantle + 20 Brisbane + 19 Melbourne = 86 lines**.
Product sequence numbers restart by destination, so sequence alone is not a
safe deduplication key. Preserve sheet row, section and item code together.

The RFQ's current intake status is `confirmed`, with a null intake warning.
Its source reference is extraction copy 1079. RFQ status itself remains DRAFT;
intake confirmation and publication are different states.

**Downstream impact exists:** bid/quote **232**, status DRAFT, was created for
this RFQ at **19:46:02 UTC**, after confirmation. This check does not establish
its prices, whether every line was copied, or any external delivery.

## Timeline (2026-11-08 UTC)

- 19:42:53: Handler `run_33d6993408024bc5` starts extraction, receiving RFQ 562
  in `extracting` state.
- 19:45:00: one-shot review automation `sch_e8b767d6241e401a` fires. It reports
  16/102 coverage, records the missing lines and notifies user 2.
- 19:45:53: RFQ updated to its current confirmed state. The available timestamp
  is not a complete field-change audit; do not infer an actor from it.
- 19:46:02: downstream draft quote 232 created.

A search of BSS's recorded tool messages did not find a confirmation call for
RFQ 562. That narrows the evidence but **does not identify who confirmed it**.
The confirmation may have happened through the UI or another client; request or
business audit evidence is needed. Do not claim a human or agent actor without it.

## Cause: established and unresolved

The extraction result explicitly noted additional product lines omitted due to
source length. Crontal currently makes one structured-extraction model call,
limits prepared input to 60,000 characters and output persistence to 100 items,
and does not reconcile extracted rows with original spreadsheet coverage.
The exact historical raw model request/response was not recovered. Therefore
the 16-line stopping point is not proven to be the 60,000-character input cap
or any specific model limit. A local Handler MarkItDown reproduction also lacks
the XLS converter's optional dependencies; it cannot establish the production
converter's output. Do not install dependencies or call the extraction model
against customer data merely to complete this investigation.

The confirmation vulnerability is confirmed in current code:
`received_rfq_service.confirm_received_rfq` accepts any nonempty listing set
once extraction stops and clears `intake_message`. There is no coverage gate.
Even a complete 102-line model result would encounter the separate 100-item cap.

## Follow-up/repair proposal (not executed)

1. Recover actor/request evidence if retention permits; inspect draft quote 232
   before planning repairs. Warn owners of dependent records through the agreed
   product workflow; this investigation does not authorize unsolicited messages.
2. Preserve original source and the existing 16 lines. Reconcile all 102 against
   stable source-row keys. Do not blindly rerun extraction and duplicate them.
3. Design bounded row extraction, durable coverage counts and review exceptions;
   preserve warnings after confirmation. Treat 102-line inputs as normal work.
4. Present a concrete repair including quote impact before changing production
   status, appending lines or repricing quotes.

Initial source/count/downstream investigation is complete. Historical actor and
exact model omission mechanism remain open. Temporary sensitive evidence is in
`/tmp/bss-rfq562-investigation/`; this report contains the durable findings.
