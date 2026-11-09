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


def test_stable_prefix_fingerprint_ignores_changing_context():
    rules = PromptComponent("rules", "Keep all source facts.")
    a = [rules, PromptComponent("speaker", "Alice", True)]
    b = [rules, PromptComponent("speaker", "Bob", True)]
    assert prompt_token_breakdown(a)["stable_prefix"] == prompt_token_breakdown(b)["stable_prefix"]
    assert render_components(a) == "Keep all source facts.\n\nAlice"
    assert prompt_token_breakdown([PromptComponent("rules", "Changed")])["stable_prefix"] != prompt_token_breakdown(a)["stable_prefix"]
