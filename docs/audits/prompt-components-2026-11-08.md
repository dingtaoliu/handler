# BSS instruction token breakdown — 2026-11-08

Counts below use `o200k_base`, the local fallback encoding for `gpt-6.1-sol`. These are exact counts for that encoding of the rendered instruction text, not provider usage or billed tokens. Percentages use **instruction tokens only** as the denominator. Tool schemas, conversation history, new input, images and provider framing are excluded.

The baseline is the BSS chat prompt snapshot from the audit (12,898 characters). The reconstructed revised preview keeps the same custom persona, AI identity, memory index and mailbox hint. It uses the actual BSS gateway tool names from the audit, including Gmail, plus the new `mail_remember` and `approval_set_continuation` tools. It includes the new bounded-model, approval-continuation and mail-summary rules and has no speaker or thread-linked memory, matching the baseline preview. Production has not been changed by this measurement.

Baseline: **3,179 tokens**. Revised: **3,007 tokens**, **172 fewer (5.41%)**. This is a prompt reduction, not a forecast of total cost savings. A new `memory_kinds` tool schema adds some overhead outside these instruction counts, and retrieving templates uses tokens when necessary.

## Before

| Component | Tokens | % of instructions |
|---|---:|---:|
| persona.system | 695 | 21.86% |
| persona.identity | 25 | 0.79% |
| persona.persona | 48 | 1.51% |
| crontal_identity | 89 | 2.80% |
| memory_intro | 24 | 0.75% |
| memory_kinds_templates | 515 | 16.20% |
| memory_rules | 199 | 6.26% |
| memory_index | 1,140 | 35.86% |
| thread_guidance | 47 | 1.48% |
| memory_reminder | 11 | 0.35% |
| ask_user_rules | 100 | 3.15% |
| crontal_api_guidance | 64 | 2.01% |
| gmail_guidance | 72 | 2.26% |
| mailbox_guidance | 91 | 2.86% |
| crontal_file_guidance | 59 | 1.86% |

## Revised

| Component | Tokens | % of instructions |
|---|---:|---:|
| persona.system | 695 | 23.11% |
| persona.identity | 25 | 0.83% |
| persona.persona | 48 | 1.60% |
| crontal_identity | 89 | 2.96% |
| thread_guidance | 47 | 1.56% |
| ask_user_rules | 100 | 3.33% |
| crontal_tool_guidance | 78 | 2.59% |
| gmail_tool_guidance | 41 | 1.36% |
| integration.mail_tools | 91 | 3.03% |
| bounded_model_guidance | 73 | 2.43% |
| approval_continuation_guidance | 76 | 2.53% |
| mail_summary_guidance | 76 | 2.53% |
| memory_rules | 229 | 7.62% |
| memory_kinds | 199 | 6.62% |
| memory_index | 1,140 | 37.91% |

Counts use successive rendered-prefix token deltas, including separators, so components add to the encoded total. Rounded percentages can differ slightly from 100%. The live preview now exposes the same component accounting for current settings; live runs may additionally contain a speaker, linked memory and runtime-specific instructions.

The largest remaining instruction component is the memory index. It stays intact here to preserve quality; saved business rules also remain verbatim. Templates are available through `memory_kinds` before creating topics. Stable rules now precede the changing index, linked memory and speaker, improving the opportunity to reuse cached prefixes.
