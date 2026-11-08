"""Component accounting includes joins and is explicitly separate from API usage."""

import tiktoken

from handler.runtime.observability.prompt_tokens import (
    PromptComponent, prompt_token_breakdown, render_components,
)


def test_additive_counts_match_rendered_text_and_include_unicode():
    components = [PromptComponent("rules", "Keep facts accurate."),
                  PromptComponent("empty", ""),
                  PromptComponent("memory", "供应商说：316L, 10吨。", True)]
    counts = prompt_token_breakdown(components, model="unknown-model")
    expected = len(tiktoken.get_encoding("o200k_base").encode(render_components(components)))
    assert counts["total_tokens"] == expected == sum(c["tokens"] for c in counts["components"])
    assert counts["encoding_source"] == "fallback"
    assert counts["components"][1]["dynamic"]
    assert counts["denominator"] == "rendered_instructions"


def test_empty_and_special_token_text_are_valid():
    assert prompt_token_breakdown([])["total_tokens"] == 0
    assert prompt_token_breakdown([PromptComponent("data", "<|endoftext|>")])["total_tokens"] > 0
