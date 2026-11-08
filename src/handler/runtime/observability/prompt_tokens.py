"""Local rendered-text token accounting, separate from provider usage/billing."""

from dataclasses import dataclass
from typing import Iterable

import tiktoken


@dataclass(frozen=True)
class PromptComponent:
    name: str
    text: str
    dynamic: bool = False


def render_components(components: Iterable[PromptComponent]) -> str:
    return "\n\n".join(c.text for c in components if c.text)


def prompt_token_breakdown(components: Iterable[PromptComponent], *,
                           model: str | None = None) -> dict:
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
            "model": model, "encoding": encoding.name, "encoding_source": encoding_source,
            "method": "rendered_text_prefix_deltas", "denominator": "rendered_instructions",
            "excludes": ["tool_schemas", "conversation_history", "current_input",
                         "provider_message_framing", "images"],
            "note": "Local tokenizer counts, not provider usage or billed tokens. "
                    "Percentages describe instructions only; fallback encoding may differ from the model."}
