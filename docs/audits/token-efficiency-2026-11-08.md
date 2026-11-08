# Handler token and cost audit — November 8, 2026

## Scope and evidence

Read-only audit of the production Crontal Handler service (`crontal-agent`, release v47), its `main`, `bss`, and `yeup` instances, the personal Handler runtime on macbook-server, and the shared runtime/legacy source. Production data was collected around 06:25–06:41 America/New_York on November 8. SQL used a read-only connection and a 25-second statement timeout. No model calls, configuration changes, deployments, or business actions were made for this audit.

The reporting window is the preceding 30 days, through collection time; this is observed usage, not a projection for a full month of steady operation. API totals were checked against direct database aggregates, avoiding the API's thread/run scan limits. Local source was branch `rebuild` at `524422d`; historical behavior is established from production records rather than assumed to match every line of today's source. The production source commit was not independently fingerprinted.

Costs below are Handler's calculated USD estimates, not reconciled OpenAI invoices. Historical unpriced/abandoned runs and hosted web-search fees make these incomplete. Personal Codex usage is recorded with API-equivalent prices even though deployment notes say it uses ChatGPT login: do not add that estimate to the Crontal API bill. Raw snapshots, including potentially private tool previews, were kept under `/tmp/handler-token-audit/`, not committed here. No credentials are in this report.

## Measured usage

| Instance | Runs | Input tokens | Cached input | Output tokens | Recorded cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| BSS | 580 | 58,445,593 | 44,104,904 (75.5%) | 292,524 | $42.3322 |
| Main / Brava Canada | 82 | 4,525,361 | 3,883,043 (85.8%) | 19,520 | $1.6060 |
| Yeup | 6 | 141,883 | 102,431 (72.2%) | 1,052 | $0.1113 |
| Crontal service total | 668 | 63,112,837 | 48,090,378 | 313,096 | $44.0494 |
| Personal runtime (separate billing context) | 45 | 31,391,471 | 28,368,896 | 343,235 | $10.9194 API-equivalent |

BSS accounts for 96.1% of the Crontal service's recorded cost. Its average is 100,768 input tokens **per run**, summing all model requests within the run. This is not its average context-window occupancy.

| BSS thread type | Runs | Model requests | Input tokens | Recorded cost | Cost share |
| --- | ---: | ---: | ---: | ---: | ---: |
| Connected mailboxes | 314 | 793 | 24,325,848 | $24.8043 | 58.6% |
| Chat | 257 | 644 | 28,985,396 | $14.5163 | 34.3% |
| Automations | 9 | 70 | 5,134,349 | $3.0116 | 7.1% |

There were 1,507 recorded BSS model requests: 1,501 on `gpt-6.1-sol`, six on `gpt-5.6-sol`. Current defaults for every thread type are `gpt-6.1-sol`, reasoning `low`. No Astra requests were recorded.

For Sol requests the median input was 25,493 tokens, p95 97,646, and maximum 240,426. Eight recorded Sol request entries exceeded 200k. The workload averages 2.6 model requests per run; there is no evidence of a general runaway-loop problem.

Reconstructing the recorded costs from per-call token details and the repository price table gives:

| Billed component | Cost | Share |
| --- | ---: | ---: |
| Cache writes | $31.0481 | 73.3% |
| Cache reads | $4.4681 | 10.6% |
| Other input | $3.8740 | 9.2% |
| Output, including reasoning | $2.9420 | 6.9% |

Output shortening and lower reasoning effort have limited headroom compared with reducing repeatedly written context. Reasoning is already included in output; do not bill it twice. Cached input is cheaper, but a high cache-hit ratio alone does not establish efficient spending.

## Findings and proposed changes

### 1. Use a cheaper model for bounded tasks, with measured escalation

The same Sol model handles straightforward mailbox triage, newsletters, drafting, document analysis, report synthesis, and general chat. Three connected BSS mailboxes are enabled in **full** mode and poll every 120 seconds. Polling itself does not invoke an LLM; eligible newly received mail does.

Introduce a small, read-only triage stage with a strict output schema: category, relevance, whether attachment/history inspection is needed, evidence IDs, and escalation reason. Evaluate `gpt-6-luna` for this stage. Route ambiguous messages, business commitments, technical drawings, units/specifications, pricing, and meaningful writes to the current Sol workflow. A model's self-reported confidence is insufficient: use deterministic checks and an explicit escalation policy, plus sampled review of items classified as irrelevant.

`gpt-6.1-terra` is a candidate for routine drafting/extraction where Luna fails quality checks. The current model enum and local price table do not include it, so it requires configuration/schema/pricing work before use. Keep Sol as the initial default for complex chat and report synthesis. Do not lower every thread's model at once.

Current standard short-context rates per million tokens:

| Model | Input | Cached input | 30-minute cache write | Output |
| --- | ---: | ---: | ---: | ---: |
| gpt-6.1-sol | $2.00 | $0.10 | $2.50 | $10.00 |
| gpt-6.1-terra | $1.00 | $0.05 | $1.25 | $5.00 |
| gpt-6-luna | $0.10 | $0.01 | $0.125 | $0.50 |

Repricing **all** BSS tokens at the same measured token mix gives approximately $42.22 on Sol, $21.11 on Terra, or $2.33 on Luna. These are price-only counterfactuals, not expected savings: models can use different numbers of calls/tokens, and quality parity has not been tested. The small difference from $42.33 reflects the six historical GPT-5.6 Sol requests. Routing adds its own cost, which must be included in the evaluation.

Official sources: [pricing](https://developers.openai.com/api/docs/pricing), [model capabilities and compatibility](https://developers.openai.com/api/docs/guides/latest-model).

### 2. Reduce prompt size and keep the reusable prefix stable

The live BSS chat prompt preview is 12,898 characters **before tool schemas and thread-specific linked memory**; main is 8,753 and Yeup 5,532. BSS's stored persona fields total 3,391 characters. The rest includes memory-kind descriptions/templates, a changing memory index, identity, and tool guidance. Current defaults permit a 4,000-character index and 6,000 characters of linked memory; templates are not covered by either cap.

`build_instructions()` renders memory before speaker identity and tool guidance. Changes to recent-memory ordering/content can therefore change an early part of the instruction prefix between runs. Measure the resulting cache-write effect rather than assuming every miss is due to memory: idle gaps and new history also matter. The stored persona also describes acting with the chatting person's identity, while the later identity section says the agent acts as the merchant AI user. Resolve that conflict for correctness as well as brevity.

Proposed layout:

1. Stable identity, business rules, tool policy, and compact role instructions.
2. Stable tool schemas in deterministic order.
3. A bounded per-run context block containing the speaker, time, relevant memory, and new events.

Load memory templates on the first save of the relevant kind rather than putting every template in every read-only turn. Replace a global recent-topic index with a small set of relevant entries plus search. Preserve manufacturing restrictions and other decision-critical facts when shortening context. Select applicable integration guidance rather than adding Gmail/Crontal hints everywhere.

BSS currently exposes 44 gateway tools, plus web search and three workspace tools. There is overlap between the generic Crontal API tools and curated Crontal tools, and between Gmail and IMAP tools. Start with a stable tool subset per workflow and expand only when needed. Retain useful curated tools that save round trips; do not remove them just to minimize tool count. Avoid changing the entire tool set at each inner step, which can harm caching and tool reliability.

Source: `runtime/instructions.py`, `runtime/memory.py`, `runtime/gateway/mcp_server.py`, `runtime/harness/openai_agents.py`. OpenAI documents that [cache reuse requires matching rendered prefixes](https://developers.openai.com/api/docs/guides/prompt-caching).

### 3. Retain the audit log, but bound the model's working history

The native harness sends new input with `conversation_id`. It does **not** replay the local transcript and raw logs on every ordinary run. Local copies of tool calls/results serve persistence and observability; deleting those logs would not reduce ordinary model usage. Recovery is an exception: `_recap()` supplies at most 20 message excerpts of 500 characters each after a lost session.

However, full tool calls/results remain in the server-side model conversation and contribute to subsequent input. Server-side persistence saves client transport, not all model billing. The 500-character StepRecorder preview cap and base64 omission apply to logging, not to what the model sees. In particular, small logged outputs for image reads do not imply small image-token usage.

Compaction is configured at 200,000 tokens. Production traces demonstrate it works: the November 7 daily digest reaches 240,426 input tokens, then drops to 24,390. That run starts with 162,361 input tokens and totals 1,051,931 across nine requests. The November 6 weekly report starts at 90,842 and totals 1,214,762 across ten requests. Scheduled jobs retain prior report/tool history in one persistent automation conversation even though a watermark is already stored in memory.

Use a fresh model session for each periodic report, initialized with its schedule, last successful watermark, compact prior report state, relevant policies, and source IDs. Keep the complete durable transcript outside that session. Preserve pending approvals and continuation state where applicable. Benchmark 40k/80k/120k compaction thresholds for interactive threads rather than globally resetting chats or assuming the smallest threshold wins: compaction also costs tokens and changes cache reuse.

An older tool result should eventually become a small source reference and factual summary, with an explicit retrieval path for the original. Preserve call/output pairing, unresolved approvals, important user constraints, and exact business facts. Test session rotation/compaction through the SDK rather than deleting arbitrary call items from a live conversation.

Sources: `runtime/harness/openai_agents.py:61`, `runtime/run.py:746`, `runtime/observability/recorder.py`; [official compaction guidance](https://developers.openai.com/api/docs/guides/compaction).

### 4. Email retrieval is the largest concrete tool opportunity

| BSS tool | Calls | Logged output bytes | Largest output |
| --- | ---: | ---: | ---: |
| mail_read_thread | 93 | 3,013,461 | 145,770 |
| mail_search | 198 | 1,267,371 | 48,116 |
| mail_read | 99 | 869,633 | 33,180 |
| crontal_api_get | 86 | 418,639 | 82,757 |
| memory_read | 130 | 351,202 | 14,700 |
| read_file | 162 | 287,372 | 23,509 |

Those first three mail tools produce 75.1% of the 6,861,448 logged output bytes. Bytes are a proxy for text volume, not tokenizer measurements; image data is omitted from these logs and hosted-search snippets are not fully represented.

`mail_read_thread()` loads all conversation messages and chooses a per-message body cap using a **4,000-character minimum**. Once a thread exceeds 15 messages, that minimum alone can exceed the nominal 60k thread budget. Headers, attachment metadata, JSON wrapping, and Unicode escapes add more. Email bodies can also quote earlier messages, so a thread read can repeat text already present in the live event or another quoted body.

Observed example: `run_302e00e53a334b73` returns 145,770 logged bytes from one thread read. The first model request has 19,843 input tokens; the next has 84,764, and later requests remain around 94k. This makes the full-thread read a demonstrated context amplifier, although not every added token can be assigned to it independently.

Proposed retrieval contract:

- Default to the latest message's new text and compact thread metadata; return references to older content. Preserve quoted raw content for explicit retrieval, especially inline replies and amendments.
- Add `after_message_id`/cursor, recent-message limits, field selection, and a hard budget on the **serialized** result. Report coverage and a continuation cursor; never silently drop older messages.
- Add bounded `mail_read_many` and multi-mailbox search, deduplicate identical Message-IDs within authorized scope, and return compact results. Batch independent reads; never concatenate unbounded full bodies into a single giant response.
- Reuse the current email event instead of reading its same body again. Load older messages and attachments only when needed to answer the actual question.
- Keep per-conversation structured state so daily/weekly reports use changes since a watermark and retrieve source messages to verify decisions.

Cross-mailbox duplication exists but is secondary: 443 stored copies in the period represent 432 distinct Message-IDs; nine IDs occur across mailboxes in 20 copies. Some copies are backfill/non-waking. This does not establish nine duplicate model runs. Current ingestion deduplicates within a mailbox, not across mailboxes.

Other useful tool changes:

- Gateway `result_text()` uses `json.dumps()` with default ASCII escaping and whitespace. Use compact UTF-8 JSON for **model-facing results** after compatibility verification. Leave approval digests/canonicalization unchanged. This preserves semantics and avoids expanding Chinese into `\uXXXX` text; actual token savings need measurement.
- Generic Crontal JSON responses have a 60k-character cap; file downloads automatically return up to 20k extracted characters. Offer compact field projection and an opt-in extraction/read path. Cache extracted documents by content hash while preserving source/version references.
- `read_file` already supports sheet/range/page targeting and caps text at 20k characters, with at most four PDF page images per call. Use these existing features. Add search/outline tools for targeted reads before reducing caps blindly.
- Combine route search and relevant schema retrieval into a bounded lookup tool, or provide direct tools for frequent workflows. An application-level API catalog cache saves HTTP work but does not eliminate a model's separate routes → describe → call round trips.
- Preserve approval boundaries and idempotency for any composite write workflow.

Sources: `runtime/integrations/mailbox/tools.py:223`, `runtime/integrations/mailbox/events.py`, `runtime/gateway/gateway.py:32`, `runtime/gateway/crontal_api.py:357`, `runtime/documents.py`.

### 5. Background work: optimize wakes and approval continuations

BSS has two enabled recurring jobs: a daily customer-mail digest at 08:00 Asia/Shanghai and a Friday activity report at 14:00 America/New_York. Main has one disabled every-minute test; Yeup and the personal runtime have no current schedules. Historical BSS automation runs cost $3.01. This is not evidence of excessive recurring-job count.

The heavier background workload is full mailbox handling: 229 received-mail events, including triage of irrelevant mail, plus follow-up runs. BSS has 111 approval-resolution events and 108 runs whose first trigger is approval resolution, costing $9.4239 (22.3% of BSS cost). Of these, 88 are mailbox-thread runs costing $7.4838. This cost overlaps the thread-type table above and must not be added to it. Historical approvals include 62 executed `memory_save` approvals and 22 expired Crontal API approvals.

The gateway already executes an approved action deterministically. It then emits `approval.resolved` for all resolutions, including denied/expired ones, waking the conversation. Add an explicit continuation contract: resume the model when dependent work remains; otherwise render the receipt/status deterministically and retain an event for the next needed run. Coalesce independent approval receipts on the same thread. Do not suppress a rejection that needs an alternative response or any follow-up essential to the user's task. Historical memory approvals do not prove today's policy still requires them: current source allows append-only untrusted memory saves with provenance; verify live policy before proposing changes.

Poller, scheduler, lease heartbeat, workspace sync, and outbox loops are conventional code, not agent invocations. Increasing a poll interval mostly affects infrastructure/freshness, not token spending when no new events arrive. Prefer event-level deduplication, inexpensive triage, and durable progress over blanket disabling of requested business automation.

The native harness permits 40 inner turns, but the average observed count is low. Use task-specific ceilings and cost alerts to catch outliers without cutting off valid work. `HANDLER_MAX_CONCURRENT=20` is a concurrency cap, not 20 agents running continuously.

Sources: `runtime/scheduler.py`, `runtime/integrations/mailbox/service.py:368`, `runtime/gateway/gateway.py:315`.

### 6. Subagents are not a measured BSS cost driver

No BSS production runs have a parent run ID. Its native harness instantiates a single Agent with no configured handoffs or subagent tools. The UI's Tasks records track work; they do not establish additional model agents. There is no evidence of BSS subagent proliferation.

Legacy Handler has a separate `task` tool that spawns a background agent, inherits the main model, and allows 50 turns. The personal legacy database currently has no task rows or token-usage entries in the last 30 days. The personal rebuilt runtime recorded 45 Codex runs and no schedules. Its top-level parent-run field cannot rule out every harness-internal Codex delegation; those traces were not exhaustively audited. Native BSS findings should not be generalized to hidden Codex behavior.

The legacy code has independent efficiency issues if it is used again: time is inserted near the beginning of the system prompt, compaction triggers on **summed run input** rather than current rendered context, and session-ending memory housekeeping invokes a model. Address them within the legacy backend rather than applying those conclusions to BSS's native harness.

## Instrumentation needed before experiments

1. Persist the resolved model/effort at request start. `Run.model` is currently initialized from explicit overrides/thread values, while the harness resolves defaults later. Consequently 577 BSS runs are labeled unknown by `/api/usage`, despite their `llm_call` steps identifying Sol. Record response-reported model and service tier where available.
2. Include cache-write counts in the usage API and break costs out into reads, writes, other input, output, and hosted-tool fees. BSS logged 54 web-search calls; at the documented $10/1,000 search-call fee that suggests another $0.54, subject to invoice reconciliation and log completeness.
3. Record each request's input, output, reasoning, context size, compacted status, latency, actual model, workflow, and trigger. Add tool result token estimates/bytes and image counts. Explicitly identify aggregate fallback entries instead of assuming each usage event is one request.
4. Use SQL/date pagination for usage instead of silent scan limits. Show priced/unpriced coverage. Main has 16 unpriced runs, BSS three; zero recorded tokens for abandoned runs do not prove zero spend.
5. Audit failures from gateway results/audit rows as well as stream status. All BSS tool-step rows reported zero errors, yet gateway audit records include denied calls and tool errors, including ten invalid title-setting attempts on non-chat threads. Fix the prompt/tool applicability conflict to eliminate those calls and keep them out of success metrics.
6. Alert when configured compaction is rejected and disabled; current recovery turns it off for the harness object. The observed drops show it worked on sampled runs, not that it is guaranteed for every model.

## Implementation order and quality gates

First implement observable, semantics-preserving reductions: accurate accounting, compact result serialization, bounded/paged mail retrieval, stable prompts, applicable tool hints, and session rotation for periodic reports. Keep Sol during this stage to isolate the effects of context changes.

Next evaluate cheap read-only triage and routine drafting/extraction against the existing Sol workflow. Build a fixed, permission-controlled corpus from representative BSS tasks: multilingual inquiries, spam, unfamiliar customers, quoted replies, spreadsheets, scanned PDFs, technical exclusions, dates/units, approval continuations, and weekly/daily reporting. Replay writes against fixtures or a dry-run backend, not production. Include the current baseline's known errors rather than treating its answer as ground truth.

Measure end-to-end cost per **successfully completed task**, latency, request count, escalation and retry rate, missed actionable mail, exact extraction correctness, source coverage, duplicate records/notifications, and approval compliance. Treat silent omissions, incorrect amounts/specifications/recipients, and approval bypasses as failures. Keep a human-reviewed sample of messages the triage model would ignore. Use deterministic checks for numbers/units/schema where possible and review the rest; do not rely solely on an LLM judge or self-confidence.

Run a canary, expand only after the agreed quality thresholds hold, and retain a per-workflow rollback to Sol/full retrieval. Do not stack assumed percentage savings: prompt reduction, smaller results, caching, routing, and fewer continuations interact. A 30–50% cost-reduction goal is a reasonable experiment target, **not a measured result or a promise of unchanged quality**. There is sufficient evidence to prioritize input/context work before a global model downgrade.

## Read-only aggregate query used for the main breakdown

Run separately with the intended instance schema in `search_path` and `default_transaction_read_only=on`:

```sql
SELECT t.type, r.harness, count(*) AS runs,
       sum((r.usage->>'input_tokens')::bigint) AS input_tokens,
       sum((r.usage->>'cached_input_tokens')::bigint) AS cached_input_tokens,
       sum((r.usage->>'cache_write_tokens')::bigint) AS cache_write_tokens,
       sum((r.usage->>'output_tokens')::bigint) AS output_tokens,
       sum(r.cost_usd) AS recorded_cost_usd,
       count(*) FILTER (WHERE r.cost_usd IS NULL) AS unpriced_runs
FROM runs r JOIN threads t ON t.key = r.thread_key
WHERE r.started_at >= now() - interval '30 days'
GROUP BY t.type, r.harness;
```

Use `run_steps` with `kind='llm_call'` for actual model/request breakdowns. Do not group only by `runs.model` until resolved-model persistence is fixed.
