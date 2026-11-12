# BSS cost measurement follow-up — November 9

Read-only request-ledger inspection after v58 found **no metered model requests
since the v56 rollout** in either the main or BSS instance. The new release has
therefore not yet produced a production comparison cohort. Zero post-release
spend is not evidence of an efficiency gain. Do not manufacture customer turns
or add warming automations to fill the gap.

BSS detailed-ledger totals before v56:

| Type | Runs | Requests | Input tokens | Cached input | Output | Estimated model cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Chat | 38 | 156 | 11,568,679 | 10,405,856 | 40,756 | $4.3487 |
| Mail | 56 | 217 | 10,139,751 | 7,235,361 | 46,006 | $4.7987 |
| Automation | 2 | 15 | 634,779 | 524,339 | 4,194 | $0.2475 |

These cover only the detailed ledger's available period, not all historical BSS
usage. Counts are requests/runs, not independent tasks of equal complexity.
Costs are the existing model-token estimates and exclude external tool fees.
Cached tokens are a subset of input, never an additional usage bucket.

The next comparison should group by instance, thread type, effective model,
request/run/task, tool-result size and cache-read fraction. Match similar work
and report input/output, request count and verified outcome together; a cheaper
partial result is not a successful optimization. Use the immutable user/thread
attribution already captured. Compare saved-summary reads only for previously
fully read emails; do not reduce unread source coverage to improve the metric.

Keep the current main model and 200k compaction threshold while collecting this
cohort. Earlier compaction needs a separate synthetic regression set covering
amendments, units, source-page coverage, unresolved questions and linked actions.

The additive Crontal catalogue release also adds an optional shape filter. A live
read of just `pipe,hollow_section_square` returns **2,549 bytes**, versus **13,725
bytes** for the full 19-shape catalogue: **81.4% smaller**. This is a measured
response-size reduction, not a before/after task billing claim. The default full
response remains available; agents can request only the shapes they need.

The asynchronous endpoint audit found only the received-RFQ create/retry business
jobs returning HTTP 202; those resource IDs are already correlated by the task
outcome adapter. The legacy MCP transport's 202 acknowledgement is not another
business job. No additional job adapter or polling automation is justified by
that endpoint inventory. Unknown future async receipts remain unverified.

## November 12 update: a production cohort now exists

This supersedes the opening paragraph's November 9 observation. Read-only ledger
inspection on November 12 found **1,238 BSS model requests across 190 distinct
runs** after v56's `2026-11-09T11:42:09Z` release, through the latest captured
request `2026-11-12T11:49:32Z`. Main has no metered requests in that interval.
BSS totals: **60,047,501 input tokens**, including **51,103,073 cached input**
(85.1%), **265,582 output tokens**, and **$19.6387 estimated model spend**.

| Post-v56 work | Runs | Requests | Input | Cached input | Output | Estimated cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Automation / Terra | 61 | 462 | 14,608,740 | 12,441,682 | 91,460 | $3.7879 |
| Chat messages / Sol | 45 | 306 | 22,832,938 | 20,538,318 | 85,827 | $8.5964 |
| Chat answer resumptions / Sol | 5 | 31 | 1,135,431 | 958,772 | 7,047 | $0.6079 |
| Bounded helpers within chat / Terra | 3 | 4 | 37,900 | 0 | 5,317 | $0.0737 |
| New mail / Terra | 76 | 425 | 20,761,594 | 16,742,175 | 75,214 | $6.2371 |
| Mail approval resumptions / Terra | 3 | 10 | 670,898 | 422,126 | 717 | $0.3356 |

Helper runs overlap parent chat runs: do not add the Runs column to get a distinct
count. These are ledger model estimates, not provider invoice reconciliation.
There is **no evidence here of excessive helpers**: four requests, $0.0737,
about 0.38% of model spend. No additional subagents were launched for this audit.

### Matched model/trigger comparisons

| Cohort | Runs | Requests/run | Input/run | Cache-read share | Cost/run |
| --- | ---: | ---: | ---: | ---: | ---: |
| Before v56, chat.message / Sol | 31 | 4.61 | 347,011 | 89.90% | $0.1310 |
| After v56, chat.message / Sol | 45 | 6.80 | 507,399 | 89.95% | $0.1910 |
| Before v56, mail.received / Terra | 44 | 3.70 | 196,744 | 71.12% | $0.0815 |
| After v56, mail.received / Terra | 76 | 5.59 | 273,179 | 80.64% | $0.0821 |

Chat cost/run is about 46% higher; Terra mail cost/run is roughly flat (+0.7%).
Mail caching improved, while requests and aggregate input/run increased. A run's
input total sums all requests and is not context-window occupancy. These groups
match model and trigger, **not task complexity, thread age, source volume or
verified outcome**. Multiple fixes shipped around the cutoff. Do not attribute
the differences causally to prefix caching or claim overall savings.

### Automation and tool context targets

A new schedule, **“Auto-file new inquiries and purchase orders”**, created
November 9 at 23:48 UTC, runs **every hour**. It accounts for 60 runs, 451 requests
and **$3.6312** in this cohort (about 18.5% of model spend). This workload did not
exist in the earlier baseline. The weekly summary has no captured firings in
this interval; the old one-shot RFQ review is disabled. The hourly schedule was
not disabled or changed: assess what each run finds before replacing its business
behavior with a deterministic new-mail/checkpoint gate. Preserve pending work,
amendments and processing receipts when designing that gate.

There were 2,438 recorded tool calls, producing 6,908,130 bytes and **2,191,172
locally estimated fresh text-output tokens**. The largest sources are:

| Tool | Calls | Estimated output tokens | Share of fresh tool-output tokens |
| --- | ---: | ---: | ---: |
| crontal_api_get | 310 | 679,089 | 31.0% |
| mail_read | 108 | 512,133 | 23.4% |
| memory_read | 479 | 348,191 | 15.9% |
| mail_search | 256 | 135,347 | 6.2% |
| mail_get_attachment | 57 | 82,814 | 3.8% |
| mail_list_drafts | 22 | 77,357 | 3.5% |
| mail_read_thread | 40 | 58,876 | 2.7% |
| crontal_api_call | 136 | 56,008 | 2.6% |
| task_report_outcome | 326 | 37,325 | 1.7% |
| crontal_api_describe | 74 | 34,469 | 1.6% |
| Other | — | 169,563 | 7.7% |

These percentages are **not shares of billed input**: tool outputs can be read
again in later turns, cached, projected, or compacted. Do not add these estimates
to provider usage/cost. The next useful audit is repeated API/memory reads and
empty hourly firings, tied to verified outcomes. Keep full unread-mail context;
only reuse a saved summary after a prior full read, as already implemented.

Crontal v124's source-coverage audit is deliberately opt-in. Normal RFQ reads get
a compact status/count summary rather than hundreds of row receipts. This avoids
creating a new repeated-context cost while adding the correctness safeguard.
Operational read-only scripts/snapshots: `/tmp/bss-coverage-release/` (protected,
not a repository artifact). No synthetic customer model turns were generated.
