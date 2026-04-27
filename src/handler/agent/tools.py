"""SDK-agnostic tool abstraction.

Provides:
- Tool dataclass: our own tool representation (no SDK imports)
- @tool decorator: generates JSON schemas from function signatures
- invoke_tool(): calls either our Tool or SDK FunctionTool objects
- build_tool_defs_for_claude/openai(): converts tools to API-specific formats
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, cast, get_type_hints

logger = logging.getLogger("handler.agent.tools")

# Python type → JSON Schema type
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


# ---------------------------------------------------------------------------
# Tool dataclass
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    """SDK-agnostic tool representation."""

    name: str
    description: str
    params_json_schema: dict[str, Any]
    _invoke: Callable[[dict[str, Any]], Awaitable[str]]

    async def invoke(self, input_data: dict[str, Any]) -> str:
        return await self._invoke(input_data)


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------


def _parse_google_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """Extract description and per-param descriptions from a Google-style docstring."""
    if not doc:
        return "", {}

    lines = doc.strip().splitlines()
    desc_lines: list[str] = []
    param_descs: dict[str, str] = {}
    in_args = False
    current_param: str | None = None

    for line in lines:
        stripped = line.strip()

        if stripped in ("Args:", "Arguments:", "Parameters:"):
            in_args = True
            continue
        if in_args and re.match(r"^[A-Z]\w*:", stripped):
            # Hit another section (Returns:, Raises:, etc.)
            in_args = False
            continue

        if in_args:
            # "param_name: description" or "param_name (type): description"
            m = re.match(r"(\w+)(?:\s*\([^)]*\))?\s*:\s*(.*)", stripped)
            if m:
                param_name = m.group(1)
                current_param = param_name
                param_descs[param_name] = m.group(2).strip()
            elif current_param is not None and stripped:
                # Continuation line
                param_descs[current_param] += " " + stripped
        else:
            desc_lines.append(stripped)

    description = " ".join(desc_lines).strip()
    return description, param_descs


def _build_schema(func: Callable) -> dict[str, Any]:
    """Build a JSON schema from a function's signature and type hints."""
    sig = inspect.signature(func)
    hints = get_type_hints(func)
    _, param_descs = _parse_google_docstring(func.__doc__ or "")

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue

        hint = hints.get(name, str)
        json_type = _TYPE_MAP.get(hint, "string")

        prop: dict[str, Any] = {"type": json_type}
        if name in param_descs:
            prop["description"] = param_descs[name]

        properties[name] = prop

        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def tool(func: Callable | None = None, *, name: str | None = None) -> Tool | Callable:
    """Decorator that creates a Tool from a plain function.

    Usage::

        @tool
        def my_tool(arg: str) -> str:
            '''Tool description.'''
            return "result"

        @tool(name="custom_name")
        def another(arg: str) -> str: ...
    """

    def _wrap(fn: Callable) -> Tool:
        tool_name = name if name is not None else fn.__name__
        doc = fn.__doc__ or ""
        description, _ = _parse_google_docstring(doc)
        if not description:
            description = doc.strip().split("\n")[0] if doc.strip() else tool_name
        schema = _build_schema(fn)
        is_async = inspect.iscoroutinefunction(fn)

        async def _invoke(input_data: dict[str, Any]) -> str:
            if is_async:
                result = await fn(**input_data)
            else:
                result = await asyncio.to_thread(fn, **input_data)
            return result if isinstance(result, str) else str(result)

        return Tool(
            name=tool_name,
            description=description,
            params_json_schema=schema,
            _invoke=_invoke,
        )

    if func is not None:
        # @tool without parens
        return _wrap(func)
    # @tool(...) with parens
    return _wrap


# ---------------------------------------------------------------------------
# Unified tool invocation
# ---------------------------------------------------------------------------


def _is_function_tool(obj: Any) -> bool:
    """Check if obj is an SDK FunctionTool or our Tool (has schema + invocable)."""
    return hasattr(obj, "params_json_schema") and (
        hasattr(obj, "on_invoke_tool") or isinstance(obj, Tool)
    )


async def invoke_tool(
    tool_obj: Any, name: str, tool_call_id: str, input_data: dict
) -> str:
    """Call a tool, handling both our Tool and SDK FunctionTool objects."""
    if isinstance(tool_obj, Tool):
        return await tool_obj.invoke(input_data)

    # SDK FunctionTool — lazy import to isolate the dependency
    if hasattr(tool_obj, "on_invoke_tool"):
        from agents.tool_context import ToolContext
        from agents.usage import Usage

        ctx = ToolContext(
            context=None,
            usage=Usage(),
            tool_name=name,
            tool_call_id=tool_call_id,
            tool_arguments=json.dumps(input_data),
        )
        result = await tool_obj.on_invoke_tool(ctx, json.dumps(input_data))
        return result if isinstance(result, str) else str(result)

    raise TypeError(f"Unknown tool type: {type(tool_obj)}")


# ---------------------------------------------------------------------------
# Tool definition builders (API-specific formats)
# ---------------------------------------------------------------------------


def _clean_schema(schema: dict) -> dict:
    """Remove keys that Claude doesn't accept in input_schema."""
    schema = dict(schema)
    schema.pop("title", None)
    schema.pop("additionalProperties", None)
    for prop in schema.get("properties", {}).values():
        if isinstance(prop, dict):
            prop.pop("title", None)
    return schema


def build_tool_defs_for_claude(tools: list) -> tuple[list[dict], dict[str, Any]]:
    """Convert tool objects to Claude API tool definitions.

    Returns (tool_defs, lookup) where lookup maps name → tool object.
    Silently skips non-function tools (e.g. WebSearchTool).
    """
    defs: list[dict] = []
    lookup: dict[str, Any] = {}
    for t in tools:
        if not _is_function_tool(t):
            logger.debug(f"skipping non-function tool: {getattr(t, 'name', t)}")
            continue
        schema = _clean_schema(t.params_json_schema)
        defs.append(
            {
                "name": t.name,
                "description": t.description,
                "input_schema": schema,
            }
        )
        lookup[t.name] = t
    return defs, lookup


def build_tool_defs_for_openai(tools: list) -> tuple[list[Any], dict[str, Any]]:
    """Convert tool objects to OpenAI Chat Completions API tool definitions.

    Returns (tool_defs, lookup) where lookup maps name → tool object.
    Silently skips non-function tools (e.g. WebSearchTool).
    """
    from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

    defs: list[ChatCompletionToolParam] = []
    lookup: dict[str, Any] = {}
    for t in tools:
        if not _is_function_tool(t):
            logger.debug(f"skipping non-function tool: {getattr(t, 'name', t)}")
            continue
        defs.append(
            cast(
                ChatCompletionToolParam,
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.params_json_schema,
                    },
                },
            )
        )
        lookup[t.name] = t
    return defs, lookup
