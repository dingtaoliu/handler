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

Released as v56 (`39baef4`, image `647f2f3e823d`), backup b034, rollback v55.
All three instance health/auth checks passed. Read-only live BSS user 2 prompt
previews have the same 3,613-token stable prefix for chat and automation (15,291
characters); total instruction estimates are 4,808 and 4,833 tokens respectively.
Gmail guidance is absent. These exclude tool schemas and conversation history.

After item 7's reporting guidance, v58's live BSS preview is 4,959 instruction
tokens with a common 3,764-token prefix (75.9%). See the current
[token/% component breakdown](../audits/bss-prompt-components-2026-11-09.md).
The task-outcome component itself is 92 tokens. Snapshot differences are not
attributed entirely to that component; current catalog/memory also affect totals.

## Stable prompt cache key (openai_agents)

openai-agents 0.24 generates a `prompt_cache_key` from each `conversation_id` when
ModelSettings supplies none, so every new mail thread, scheduled firing (fresh
session) and new chat got its own key and never reused the shared ~17-18k-token
prefix (tool schemas + stable instructions): 0 cached tokens on all 134 new-thread
first requests in the BSS ledger.

`_plan()` now derives the key from the request's stable context only:
`"handler:" + sha256(json{model, harness, sorted effective tool names (plus
bounded_model_task when the harness adds it), stable instruction prefix})[:32]`.
The stable prefix is `stable_prefix_text()` in `observability/prompt_tokens.py`,
the same text the settings preview fingerprints (`stable_prefix.sha256`), so the
two cannot drift. Dynamic components (thread guidance, scheduled checkpoint
guidance, memory index, linked memory, speaker) and the trailing configured
instructions are excluded. Thread types with the same prefix therefore share a key;
a different tool inventory, stable text or model gets another. Without memory
tools the configured instructions are hashed instead. No user text is in the key.

The harness sends it as `ModelSettings(extra_args={"prompt_cache_key": key})`
(ModelSettings has no native field). The SDK's `PromptCacheKeyResolver` returns
None when `extra_args`/`extra_body` already contain `prompt_cache_key`, so it adds
no per-conversation key; the Responses model forwards `extra_args` into
`responses.create()`. It also sends `ModelSettings.prompt_cache_retention="24h"`.

Settings (`efficiency`, live, no deploy): `stable_prompt_cache_key` (default true;
false restores the SDK's per-conversation key) and `prompt_cache_retention`
(`"24h"` default, or `"in_memory"`, which omits the parameter so the request is
exactly as before and the API default applies). A 400 naming
`prompt_cache_retention` (or `prompt_cache_key`) is retried once without that
parameter and remembered per model for the process, with a warning, like refused
compaction. Codex and `bounded_model_task` helper requests are unchanged.

Unverified: whether 24h retention is priced differently from in-memory caching,
and which models accept it. Check the provider's current pricing/caching docs and
measure cached-token share on new-thread first requests after release.
