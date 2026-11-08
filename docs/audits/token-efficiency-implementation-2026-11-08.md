# Token efficiency implementation — November 8, 2026

The owner approved the six audit recommendations, with an explicit requirement that unread email context remain available in full. This change applies to the rebuilt runtime; it does not modify legacy Handler or replace the main agent model globally.

## Delivered behavior

- **Email history:** `mail_read` supports batch IDs and body offsets; `mail_read_thread` pages messages under a 60k body/summary character budget. Headers/metadata are additional to that budget. New/unread bodies are returned as original text, never automatically stripped of quotations. All pages remain retrievable.
- **Explicit summaries:** `mail_remember` requires full original-body coverage in the same thread/run, a matching source digest, and an unpredictable receipt returned only by a completed full read. The agent must provide a factual summary. Partial, changed, unread, and upstream storage-capped bodies cannot be marked fully read. Summaries are durable but thread/instance scoped and labeled untrusted source data. `force_full` retrieves originals. Existing historical mail is not bulk-marked read.
- **Scheduled sessions:** an `automation.fired` starts a fresh session without transcript replay when it has no mixed continuation events or pending approvals. Prompts explain memory checkpoints and successful watermarks. Existing approval continuations retain their session. Complete transcripts stay in Handler's store.
- **Prompts:** stable guidance precedes the changing memory index/linked memory; integration hints follow the actual tool catalog. Templates load on demand through `memory_kinds`. Custom persona/business rules remain intact. The prompt API and Handler console show a local per-component token/% table with an explicit instructions-only denominator. [Measured BSS breakdown](prompt-components-2026-11-08.md).
- **Tool results:** compact UTF-8 JSON; opt-in dot-path field projection with full originals saved; optional smaller result budgets and deferred extraction; route discovery can return up to three endpoint contracts in one call. Compatibility defaults remain available.
- **Approval continuations:** `approval_set_continuation` can mark a pending approval from the current run/thread as terminal. It does not approve or execute anything. Successful terminal actions show a deterministic receipt; failure, denial, expiration, mixed events, or nonterminal work still invoke the main agent. Skipped receipts are injected into the next model turn so the conversation does not retain stale pending status. The executor verifies the durable approval record rather than trusting an event flag alone.
- **Cheaper bounded tasks:** the native harness exposes a tool-free `bounded_model_task`: Luna classifies; Terra extracts/drafts. Sol remains the main agent and verifies evidence and material facts before acting. The helper has no history, shared memory, tools or authority to act. It does not automatically discard mail. Inputs are capped at 4k direct-text characters or five confined workspace files with a combined 50k-character budget, output at 1,500 tokens, six calls per run. File references avoid copying large sources into expensive main-model output. Unsupported/scanned/truncated sources, failed requests, absent/invalid evidence, and ambiguity return to the main agent for review.
- **Accounting:** run records persist resolved main models; usage API exposes cache-write tokens and priced/unpriced coverage. By-model summaries use batched request-step reads so helper costs are attributed to the model that made them; historical runs without request steps retain fallback attribution. Completed helper calls are metered separately, including failures/cancellation. MCP errors now preserve their error status.

## Controls and rollback

Settings → `efficiency` exposes:

| Setting | Default | Purpose |
| --- | --- | --- |
| fresh_automation_sessions | true | Rotate at a new scheduled firing, preserving pending approvals |
| bounded_model_tasks | true | Offer bounded helpers under the main agent's review |
| classification_model | gpt-6-luna | Classification helper |
| drafting_model | gpt-6.1-terra | Extraction/drafting helper |

These are live settings; disabling helpers leaves the main workflow on its existing model. Email summaries remain opt-in by the agent after full reads. No original messages or audit logs are removed. New mail tables use `CREATE TABLE IF NOT EXISTS` and are compatible with rolling back runtime code.

## Validation

- Full runtime suite: **1,122 passed, 2 skipped**, including SQLite/Postgres contracts, harness mocks, mailbox coverage/pagination/isolation, approval continuations and prompt accounting.
- Additional PostgreSQL/SQLite tests cover read-coverage gaps, stable full-read receipts, Unicode summary persistence, and thread/digest isolation.
- Docker image built for the Heroku linux/amd64 target and passed runtime import checks on Python 3.12.
- Three synthetic live API smoke requests verified actual Luna/Terra access, schema compatibility, usage capture, and preservation of an amended grade/date. Extraction selected 316L and December 15 instead of the superseded 304/December 1; the draft did not agree a price or promise delivery. These used no real customer email or business actions and cost approximately $0.002 at current listed rates.

This is functional validation, not proof of production quality parity or a measured end-to-end savings percentage. Preserve Sol's review role and compare cost per successful task, missed actionable messages, factual corrections and retries after rollout. The BSS instruction preview alone fell from 3,179 to 2,979 local fallback-encoding tokens (6.29%); tool schemas/history are excluded. The largest expected saving is avoiding old raw report history and repeated fully reviewed email bodies, which must be measured on subsequent runs.

## Deployment

Released as **Crontal Handler v48** on November 8, 2026 at 07:10 America/New_York, runtime commit `17324c3`, image `61e19295a750`. Backup **b026** completed before release; runtime rollback target is **v47**. Main, BSS and Yeup health and tool/settings endpoints passed after release. Main/BSS persona version 3 corrects only the two stale person-identity sentences, retaining the rest; version 2 is available in settings history. The personal deployment was not restarted.
