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
