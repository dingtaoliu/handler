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
accumulate stale policy snapshots, so this change does not do that. (Superseded
for per-run data, not policy: see "Instructions per thread type" below.) See the
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
first requests in the BSS ledger. A key per request prefix (c3bc3de) fixed the
routing, but the cache still could not hit while the instructions differed (below).

## Instructions per thread type, context as a user message (openai_agents)

Measured 2026-11-14 (synthetic, gpt-6.1-terra/sol): OpenAI matches cached prefixes
at block boundaries, not up to the first differing token. Tool definitions plus
`instructions` are one block: any change inside it (e.g. a different memory-index
tail after 7.3k identical tokens) gives 0 cached tokens. A first `developer`-role
input item belongs to the same block. A first `user`-role item does not: changing
it still caches the whole tools+instructions prefix. A divergence in mid or late
history falls back to the instructions boundary, not to the last matching message;
appending to an identical earlier prompt gives a near-full hit.

Before, the memory index, linked memory (with the chat context-links line) and the
speaker were rendered at the end of the instructions. Every memory change anywhere
in the instance (the index lists recently updated topics) and every change in
per-thread links made every thread's next request fully cold, and no two threads
ever shared instructions.

Now (`instructions.py`, `observability/prompt_tokens.py`, `run.py`):

- **Instructions** = f(settings, thread type, tool inventory), byte-identical for
  every thread of a type: persona, Crontal identity, thread-type guidance
  (`thread_guidance`, `scheduled_checkpoint_guidance`, now non-dynamic), ask_user,
  integration/tool hints, bounded-task, approval, mail-summary and task-outcome
  guidance, the route catalog, memory rules and kinds, a new `context_rules` line,
  then `RunConfig.instructions`. Order is fixed.
- **Context block** (components with `dynamic=True`): `memory_index`,
  `linked_memory` (including the chat context-links line) and `speaker`, wrapped
  in `<handler_context>` ... `</handler_context>` (`render_context()`), sent as a
  separate **user-role** input item before the turn (`_input()`; images stay on
  the turn's own item).
- `context_rules` (static) tells the model these blocks come from the Handler
  runtime, not the person: data (memory index, linked topics, speaker), never user
  instructions; the newest block supersedes earlier ones; memory content stays
  data under the memory rules (untrusted-topic marking is unchanged).

Offline sizes with default settings, a typical tool set and 30 one-line topics
(o200k; no persona, catalog or memory kinds): chat instructions 849 tokens /
context 331; automation 880 / 324; email 1,994 / 324 (default email
instructions); crontal 918 / 324. On BSS the instructions also hold the persona,
Crontal identity and ~1.4k-token route catalog; the context is the index plus
linked topics. Re-measure from the live preview.

### Append-only injection

History lives in an OpenAI Conversation, so an injected block stays in it. The
executor sends the block (`RunRequest.context`) only when the session does not
hold it yet:

- always on a session's first turn: a new thread, a `fresh_session` automation
  firing, a missing-session retry (`_stream` puts the full block back on the
  retried request), a Codex-to-native move or an operator session reset;
- on later turns only when `sha256(block)` differs from the last block recorded
  for that session; then the full new block (no diffs).

`threads.context_hash` holds `"<session_id>:<sha256>"` (SQLite: `ADDED_COLUMNS`;
Postgres: `schema_pg_thread_context.sql`). Because the session id is part of it, a
new session never matches; `set_session` also clears it. It is written, fenced,
only after a completed run (`_record_context`): a failed run may not have stored
its input, and repeating a block is harmless. If the harness reports that the turn
compacted the Conversation (a `compaction` output item; FINAL `compacted: True`),
the hash is cleared, so the next turn sends the block again instead of relying on
the compaction summary. Deterministic receipt turns make no model call and leave
the hash alone; approval resumptions are ordinary turns of the same session.

### Key, retention, preview, Codex

- `prompt_cache_key` = `"handler:" + sha256(json{v: 2, model, harness, sorted tool
  names (plus bounded_model_task when the harness adds it), full instructions})[:32]`:
  a hash of the system prompt, equal for every thread of a type. Different types,
  tools, settings or models get different keys. Sent through
  `ModelSettings(extra_args=...)`, so the SDK adds no per-conversation key. Switch:
  `efficiency.stable_prompt_cache_key` (default true). A 400 naming
  `prompt_cache_key` is retried once without it and remembered per model.
- `prompt_cache_retention` is removed (setting, RunPlan/RunRequest field and
  ModelSettings argument). It is deprecated in the openai client, gpt-5.5 and later
  support only 24h, and non-ZDR orgs already default to 24h. Stored `efficiency`
  values that still contain it load: a `mode="before"` validator drops the key,
  like `email.allowed_senders`.
- `/api/settings/prompt` keeps `prompt` and `token_breakdown` (everything
  combined) and adds `harness`, `context_delivery` (`user_message` or
  `instructions`), `instructions`, `context` and `parts_token_breakdown`
  (`instructions` and `context` token tables; `instructions.stable_prefix.sha256`
  is the fingerprint of the cached text). The settings page shows both parts.
- Codex is unchanged: it still gets one combined instruction text
  (`render_components()`, per-run sections last) and no context item or
  `context_rules` line. The only difference is that thread-type guidance now sits
  with the other static sections instead of after them.

Risk: memory, linked topics and the speaker now reach the model as user-role
input instead of system instructions. The markers, `context_rules` and the
existing untrusted-topic marking frame it as data. Watch for the model treating
memory text as a user request, and for the speaker line carrying less weight than
before.
