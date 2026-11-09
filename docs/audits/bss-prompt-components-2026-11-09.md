# BSS instruction token breakdown — November 9, 2026

Read-only live preview for BSS user 2, chat, Handler v58. Local tokenizer estimates of rendered instructions only; these are not provider billing or cache-hit attribution.

| Component | Tokens | % of instructions |
| --- | ---: | ---: |
| `persona.system` | 667 | 13.45% |
| `persona.identity` | 25 | 0.50% |
| `persona.persona` | 48 | 0.97% |
| `crontal_identity` | 89 | 1.79% |
| `ask_user_rules` | 100 | 2.02% |
| `crontal_tool_guidance` | 126 | 2.54% |
| `integration.mail_tools` | 142 | 2.86% |
| `bounded_model_guidance` | 73 | 1.47% |
| `approval_continuation_guidance` | 76 | 1.53% |
| `mail_summary_guidance` | 76 | 1.53% |
| `task_outcome_guidance` | 92 | 1.86% |
| `crontal_route_catalog` | 1,821 | 36.72% |
| `memory_rules` | 230 | 4.64% |
| `memory_kinds` | 199 | 4.01% |
| `thread_guidance` | 47 | 0.95% |
| `memory_index` | 1,142 | 23.03% |
| `speaker` | 6 | 0.12% |
| **Total** | **4,959** | **100%** |

The contiguous common prefix is **3,764 tokens** (75.9% of instructions). Chat and automation have identical prefix fingerprints. Dynamic thread guidance, memory and speaker follow it.

Excludes tool schemas, conversation history, current input, provider framing and images. The earlier audit found around 100k input tokens per request in a long thread; instruction reuse alone does not remove that history. Use the request ledger for billed-model estimates and cache reads. Tool-result context estimates remain separate and non-additive.

The task-outcome instruction component accounts for 92 tokens (1.86%) in this snapshot. Its two tool schemas and optional report round trip also have a cost; this is a quality/visibility tradeoff, not a token-saving claim. No model downgrade, forced email summarization, new subagent or recurring automation was introduced.
