"""Local rendered-text token accounting, separate from provider usage/billing."""

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from typing import AbstractSet, Iterable

import tiktoken


@dataclass(frozen=True)
class PromptComponent:
    """One section of a run's prompt.

    `dynamic` marks per-run content (memory index, linked memory, speaker):
    on a harness that takes a context message it leaves the instructions and is
    sent as a user-role `<handler_context>` input item, so the instructions stay
    byte-identical across runs and threads of a thread type. Everything else
    (including per-type guidance) is instruction text.
    """

    name: str
    text: str
    dynamic: bool = False


def _split(components: Iterable[PromptComponent]) -> tuple[list[PromptComponent], int]:
    """Non-empty components, instruction ones first, and the first dynamic index."""
    components = [c for c in components if c.text]
    ordered = [c for c in components if not c.dynamic] + [c for c in components if c.dynamic]
    return ordered, sum(1 for c in ordered if not c.dynamic)


def stable_prefix_text(components: Iterable[PromptComponent]) -> str:
    """The rendered instructions: every non-dynamic component, in order.

    The single source of the instructions text: the preview's fingerprint, the
    prompt cache key and the request's instructions all use it, so they cannot
    drift apart.
    """
    components, boundary = _split(components)
    return _render_stable(tuple(c.text for c in components[:boundary]))


render_instructions = stable_prefix_text


def render_components(components: Iterable[PromptComponent]) -> str:
    """Everything as one instruction text (instructions, then per-run sections).

    For harnesses without a context message (Codex), and the combined preview.
    """
    components, boundary = _split(components)
    stable = stable_prefix_text(components)
    return "\n\n".join(p for p in (stable, "\n\n".join(
        c.text for c in components[boundary:])) if p)


CONTEXT_OPEN, CONTEXT_CLOSE = "<handler_context>", "</handler_context>"


def context_components(components: Iterable[PromptComponent]) -> list[PromptComponent]:
    """The context message's parts: the markers around the dynamic components.

    Empty when there is no per-run content. Rendering them with blank-line
    joins gives exactly `render_context()`, so token accounting adds up.
    """
    dynamic = [c for c in components if c.text and c.dynamic]
    if not dynamic:
        return []
    return [PromptComponent("context_open", CONTEXT_OPEN, True), *dynamic,
            PromptComponent("context_close", CONTEXT_CLOSE, True)]


def render_context(components: Iterable[PromptComponent]) -> str:
    """The per-run `<handler_context>` block, or "" when there is none."""
    return "\n\n".join(c.text for c in context_components(components))


def context_digest(context: str) -> str:
    """Fingerprint of a context block, to inject it again only when it changed."""
    return hashlib.sha256(context.encode()).hexdigest()


PROMPT_CACHE_KEY_PREFIX = "handler:"


def prompt_cache_key(instructions: str, *, tools: AbstractSet[str] | None,
                     model: str | None, harness: str | None = None) -> str:
    """An OpenAI prompt_cache_key: a hash of the request's cached block.

    Tool definitions and instructions form one cache block, and the
    instructions hold no per-run content, so the key is the hash of the full
    instructions, the sorted tool names, the model and the harness. Every
    thread of a type with the same settings and tools shares it. The key is a
    digest: no instruction or user text appears in it.
    """
    payload = json.dumps({
        "v": 2, "model": str(model or ""), "harness": harness or "",
        "tools": None if tools is None else sorted(tools), "instructions": instructions,
    }, sort_keys=True, ensure_ascii=False)
    return PROMPT_CACHE_KEY_PREFIX + hashlib.sha256(payload.encode()).hexdigest()[:32]


@lru_cache(maxsize=32)
def _render_stable(texts: tuple[str, ...]) -> str:
    """Bounded, content-keyed compilation only; provider caching is independent."""
    return "\n\n".join(texts)


def prompt_token_breakdown(components: Iterable[PromptComponent], *,
                           model: str | None = None,
                           denominator: str = "rendered_instructions") -> dict:
    """Attribute tokens by successive prefix deltas, including separators.

    Independent section counts do not add up because BPE merges can cross
    boundaries. Prefix deltas exactly sum to the encoding of the rendered text.
    A fallback encoding measures text consistently; it is not an API usage claim.
    """
    try:
        encoding = tiktoken.encoding_for_model(model) if model else tiktoken.get_encoding("o200k_base")
        encoding_source = "model" if model else "fallback"
    except KeyError:
        encoding = tiktoken.get_encoding("o200k_base")
        encoding_source = "fallback"
    components, _ = _split(components)
    stable_text = stable_prefix_text(components)
    prefix = ""
    previous = 0
    rows = []
    for component in components:
        if not component.text:
            continue
        prefix += ("\n\n" if prefix else "") + component.text
        count = len(encoding.encode(prefix, disallowed_special=()))
        rows.append({"name": component.name, "tokens": count - previous,
                     "characters": len(component.text), "dynamic": component.dynamic})
        previous = count
    for row in rows:
        row["percent"] = round(row["tokens"] * 100 / previous, 2) if previous else 0.0
    return {"total_tokens": previous, "components": rows,
            "stable_prefix": {"sha256": hashlib.sha256(stable_text.encode()).hexdigest(),
                              "characters": len(stable_text),
                              "tokens": len(encoding.encode(stable_text, disallowed_special=()))},
            "model": model, "encoding": encoding.name, "encoding_source": encoding_source,
            "method": "rendered_text_prefix_deltas", "denominator": denominator,
            "excludes": ["tool_schemas", "conversation_history", "current_input",
                         "provider_message_framing", "images"],
            "note": "Local tokenizer counts, not provider usage or billed tokens. "
                    "Percentages describe this text only; fallback encoding may differ from the model."}


def prompt_parts_breakdown(components: Iterable[PromptComponent], *,
                           model: str | None = None) -> dict:
    """Separate accounting of the cached instructions and the per-run context message."""
    components = list(components)
    return {
        "instructions": prompt_token_breakdown(
            [c for c in components if not c.dynamic], model=model),
        "context": prompt_token_breakdown(
            context_components(components), model=model, denominator="rendered_context"),
    }
