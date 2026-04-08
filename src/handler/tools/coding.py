"""Coding tools: search_codebase, edit_file."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from agents import function_tool

from ..paths import PACKAGE_DIR as _PACKAGE_DIR, PROJECT_ROOT as _PROJECT_ROOT

logger = logging.getLogger("handler.tools.coding")


@function_tool
def search_codebase(pattern: str, path: str = "") -> str:
    """Search handler source code with regex. Returns matching lines with file paths and line numbers.

    Args:
        pattern: Regex pattern (passed to grep -E).
        path:    Optional subdirectory relative to handler/ (e.g. "channels", "actions").
    """
    search_dir = _PACKAGE_DIR / path if path else _PACKAGE_DIR
    if not search_dir.is_dir():
        return f"Directory not found: handler/{path}"

    try:
        result = subprocess.run(
            ["grep", "-rn", "-E", pattern, str(search_dir),
             "--include=*.py", "--color=never"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "Error: search timed out after 10 seconds."
    except FileNotFoundError:
        return "Error: grep not found on this system."

    if result.returncode == 1:
        return f"No matches for pattern: {pattern}"
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"

    output = result.stdout
    try:
        output = output.replace(str(_PROJECT_ROOT) + "/", "")
    except Exception:
        pass

    lines = output.strip().split("\n")
    if len(lines) > 100:
        output = "\n".join(lines[:100]) + f"\n\n... ({len(lines) - 100} more matches truncated)"

    logger.info(f"search_codebase: '{pattern}' in handler/{path or ''} → {len(lines)} matches")
    return output


@function_tool
def edit_file(path: str, search: str, replace: str) -> str:
    """Find and replace text in a file. Performs exact string match (not regex). Fails if the search string is not found or matches more than once. Auto-creates a git checkpoint when modifying handler source files.

    Args:
        path:    Path relative to project root (e.g. "handler/agent.py") or absolute.
        search:  Exact text to find (can be multi-line).
        replace: Replacement text.
    """
    p = Path(path)
    if not p.is_absolute():
        p = (_PROJECT_ROOT / path).resolve()
    else:
        p = p.resolve()

    if not p.exists():
        return f"File not found: {path}"
    if not p.is_file():
        return f"Not a file: {path}"

    # Git checkpoint for handler source files
    is_handler_file = False
    try:
        p.relative_to(_PACKAGE_DIR)
        is_handler_file = True
    except ValueError:
        pass

    if is_handler_file:
        try:
            p.relative_to(_PACKAGE_DIR / "watchdog")
            return "Blocked: watchdog/ is the recovery engine and cannot be modified here."
        except ValueError:
            pass

        from .selfmod import _git_checkpoint
        _git_checkpoint(
            str(_PROJECT_ROOT),
            f"checkpoint: before editing {p.relative_to(_PROJECT_ROOT)}",
        )

    try:
        content = p.read_text()
    except Exception as e:
        return f"Error reading {path}: {e}"

    count = content.count(search)
    if count == 0:
        return (
            f"Search string not found in {path}. "
            "Make sure the text matches exactly (including whitespace and indentation)."
        )
    if count > 1:
        return (
            f"Search string found {count} times in {path}. "
            "Provide a longer/more specific search string to match exactly one location."
        )

    new_content = content.replace(search, replace, 1)

    try:
        p.write_text(new_content)
    except Exception as e:
        return f"Error writing {path}: {e}"

    rel = path if not Path(path).is_absolute() else str(p.relative_to(_PROJECT_ROOT))
    checkpoint_note = " (git checkpoint created)" if is_handler_file else ""
    logger.info(f"edit_file: patched {rel}{checkpoint_note}")
    return f"Patched {rel}: replaced 1 occurrence ({len(search)} → {len(replace)} chars).{checkpoint_note}"
