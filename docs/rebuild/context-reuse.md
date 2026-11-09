# Instruction reuse and smaller mail-search context

Item 5 implements a bounded improvement without changing history retention,
models, email read semantics or the 200k compaction threshold.

Common persona, integration rules, route catalogue and memory rules/kinds now
precede thread-specific guidance, fresh memory and speaker identity. Chat and
scheduled firings with the same effective profile/tools share the exact common
prefix. Every run still reloads settings and memory. A content-keyed, 32-entry
process-local cache reuses compilation of that prefix; it is neither a provider
cache handle nor a substitute for sending instructions.

Run instructions and the prompt preview use the requesting/acting user's effective
tool inventory. Disconnected Gmail no longer adds Gmail instructions. Availability
is resolved per request, never globally cached across users.

The prompt token breakdown adds `stable_prefix` with SHA-256, character count and
local token count. Existing component token/% accounting remains additive. These
are local instruction measurements, not attribution of provider cache hits or a
bill. Tool schemas, history, current input, framing and images remain excluded.

`mail_search(fields=["from", "to"])` returns a smaller header view for contact
lookups. `id` and `mailbox_id` are always retained for original-source reads.
Omitting fields preserves the full existing search response. Pagination is
unchanged. This option neither removes stored content nor marks messages fully
read; `mail_read` and `force_full` retain their existing contracts.

Provider-managed implicit caching remains enabled. Existing audit samples already
showed 87.2% cached input; a local string cache cannot eliminate historical input
or its read cost. The current Agents SDK sends system instructions through the
top-level `instructions` field, where explicit text cache breakpoints cannot be
placed. Moving changing instructions into persisted Conversation input would
accumulate stale policy snapshots, so this change does not do that. See the
[official caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
and the item 5 design for the examined alternatives.

Validation covers shared chat/automation prefixes, fresh memory and changed
settings, instruction token totals/fingerprints, effective run authority, optional
mail projection and subsequent original-body reads. The synthetic contact-search
fixture's serialized result shrinks by more than half; this is not a production
billing reduction claim. Measure new request ledger totals/cache reads and mail
result token estimates against comparable tasks before claiming savings.

Follow-ups: evaluate earlier task checkpoints/compaction against source-coverage
and multi-turn quality tests. No new warming automations or forced summaries were
introduced. Historical conversations are not rewritten.
