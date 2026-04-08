"""Built-in tools: read_file, write_file, shell, web_search."""

from __future__ import annotations

import logging
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from agents import function_tool, WebSearchTool

from ..paths import (
    PACKAGE_DIR as _PACKAGE_DIR,
    PROJECT_ROOT as _PROJECT_ROOT,
    SHELL_LOG_DIR as _SHELL_LOG_DIR,
)

logger = logging.getLogger("handler.tools.builtin")


def _resolve_path(path: str) -> Path:
    """Resolve a path relative to project root, or absolute."""
    p = Path(path)
    if not p.is_absolute():
        return (_PROJECT_ROOT / path).resolve()
    return p.resolve()


@function_tool
def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Read a file from disk. Handles PDFs, DOCX, XLSX, PPTX, CSV, HTML, images, and plain text/code. Use start_line/end_line to read a specific range (adds line numbers).

    Args:
        path:       File path (relative to project root or absolute).
        start_line: First line to read (1-based). 0 = start from beginning.
        end_line:   Last line to read (inclusive). 0 = read to end of file.
    """
    from markitdown import MarkItDown

    p = _resolve_path(path)
    if not p.exists():
        logger.warning(f"read_file: not found: {path}")
        return f"File not found: {path}"
    try:
        md = MarkItDown()
        result = md.convert(str(p))
        text = result.text_content
        if not text or not text.strip():
            logger.warning(f"read_file: no text extracted from {path}")
            return f"File converted but no text content extracted: {path}"
    except Exception as e:
        logger.error(f"read_file failed: {path} → {e}", exc_info=True)
        return f"Error reading {path}: {e}"

    # Apply line range if requested
    if start_line > 0 or end_line > 0:
        lines = text.splitlines(keepends=True)
        total = len(lines)
        start = max(1, start_line) if start_line > 0 else 1
        end = min(end_line, total) if end_line > 0 else total

        if start > total:
            return f"File has {total} lines, but start_line={start_line} is beyond the end."

        selected = lines[start - 1 : end]
        width = len(str(end))
        numbered = "".join(
            f"{i:{width}d}  {line}" for i, line in enumerate(selected, start=start)
        )
        if numbered and not numbered.endswith("\n"):
            numbered += "\n"

        try:
            rel = str(p.relative_to(_PROJECT_ROOT))
        except ValueError:
            rel = path
        header = f"# {rel}  (lines {start}-{end} of {total})\n"
        text = header + numbered

    if len(text) > 20000:
        text = text[:20000] + "\n\n... (output truncated at 20k chars)"

    logger.info(f"read_file: {path} ({len(text)} chars)")
    return text


@function_tool
def write_file(path: str, content: str) -> str:
    """Write content to a file. For targeted edits, prefer edit_file(). Auto-creates a git checkpoint when modifying handler source files.

    Args:
        path:    File path (relative to project root or absolute).
        content: Full file content to write.
    """
    p = _resolve_path(path)

    # Guard handler source files with git checkpoint
    is_handler_file = False
    try:
        p.relative_to(_PACKAGE_DIR)
        is_handler_file = True
    except ValueError:
        pass

    if is_handler_file:
        try:
            p.relative_to(_PACKAGE_DIR / "watchdog")
            return (
                "Blocked: watchdog/ is the recovery engine and cannot be modified here."
            )
        except ValueError:
            pass

        from .selfmod import _git_checkpoint

        _git_checkpoint(
            str(_PROJECT_ROOT),
            f"checkpoint: before writing {p.relative_to(_PROJECT_ROOT)}",
        )

    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(content)
    except Exception as e:
        logger.error(f"write_file failed: {path} → {e}", exc_info=True)
        return f"Error writing {path}: {e}"

    try:
        rel = str(p.relative_to(_PROJECT_ROOT))
    except ValueError:
        rel = path
    checkpoint_note = " (git checkpoint created)" if is_handler_file else ""
    logger.info(f"write_file: {rel} ({len(content)} chars){checkpoint_note}")
    return f"Written {len(content)} chars to {rel}.{checkpoint_note}"


@function_tool
def shell(command: str, timeout: int = 60) -> str:
    """Run a shell command and return stdout + stderr. For Python, use: python3 -c 'code here'.

    Output is also persisted to a log file for later reference.

    Args:
        command: Shell command to execute.
        timeout: Max seconds to wait (default 60).
    """
    logger.info(f"shell: {command}")

    # Generate unique log path
    _SHELL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_id = uuid.uuid4().hex[:8]
    log_path = _SHELL_LOG_DIR / f"{ts}_{log_id}.log"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        # Persist full output to log file
        with open(log_path, "w") as f:
            f.write(f"$ {command}\n")
            f.write(f"# exit code: {result.returncode}\n")
            f.write(f"# timestamp: {datetime.now().isoformat()}\n\n")
            if result.stdout:
                f.write(result.stdout)
            if result.stderr:
                f.write("\n--- stderr ---\n" + result.stderr)

        # Build output for agent context
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += (
                ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
            )
        if not output:
            output = f"(no output, exit code {result.returncode})"
        elif result.returncode != 0:
            output += f"\n(exit code {result.returncode})"

        logger.info(
            f"shell: exit={result.returncode}, output={len(output)} chars, log={log_path.name}"
        )
        return output[:20000] + f"\n\n[log: {log_path.name}]"
    except subprocess.TimeoutExpired:
        logger.error(f"shell: timed out after {timeout}s: {command}")
        with open(log_path, "w") as f:
            f.write(f"$ {command}\n# TIMED OUT after {timeout}s\n")
        return f"Error: command timed out after {timeout} seconds\n\n[log: {log_path.name}]"
    except Exception as e:
        logger.error(f"shell failed: {command} → {e}")
        return f"Error: {e}"


web_search = WebSearchTool()


def memory_tool(mem):
    """Create a single memory tool wired to the Memory instance."""

    @function_tool
    def memory(
        action: str,
        topic: str = "",
        content: str = "",
        description: str = "",
        new_topic: str = "",
    ) -> str:
        """Persist knowledge across conversations. The memory index is in your system prompt — use read to load full content.

        Actions: save, read, rewrite, delete, help.

        Args:
            action:      One of: save, read, rewrite, delete, help.
            topic:       Topic filename (e.g. 'user.md', 'tax_situation.md').
            content:     (save, rewrite) Text to store.
            description: (save, rewrite) Short description for the index.
            new_topic:   (rewrite) New topic name if renaming.
        """
        if action == "help":
            return (
                "memory — persist knowledge across conversations.\n\n"
                "Actions:\n"
                "  save    — Create new topic or append to existing. Updates index description.\n"
                "            Required: topic, content, description.\n"
                "  read    — Load full content of a topic.\n"
                "            Required: topic.\n"
                "  rewrite — Full replace of content, description, and optionally topic name.\n"
                "            Required: topic, content, description. Optional: new_topic.\n"
                "  delete  — Remove a topic and its index entry.\n"
                "            Required: topic.\n"
            )

        if action == "save":
            if not topic or not content or not description:
                return "Missing required fields: topic, content, description."
            mode = mem.save(topic, content, description)
            return f"Memory {mode}: {topic}"

        if action == "read":
            if not topic:
                return "Missing required field: topic."
            text = mem.read(topic)
            if not text:
                return f"Topic not found: {topic}"
            logger.info(f"memory read: {topic} ({len(text)} chars)")
            return text

        if action == "rewrite":
            if not topic or not content or not description:
                return "Missing required fields: topic, content, description."
            mode = mem.rewrite(topic, content, description, new_topic)
            return f"Memory {mode}: {new_topic or topic}"

        if action == "delete":
            if not topic:
                return "Missing required field: topic."
            if mem.delete(topic):
                return f"Deleted topic: {topic}"
            return f"Topic not found: {topic}"

        return f"Unknown action '{action}'. Use: save, read, rewrite, delete, help."

    return memory
