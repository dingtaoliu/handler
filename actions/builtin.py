"""Built-in tools: read_file, write_file, write_and_run, run_python, run_shell, web_search."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from pathlib import Path

from agents import function_tool, WebSearchTool

logger = logging.getLogger("handler.actions.builtin")


@function_tool
def read_file(path: str) -> str:
    """Read a file from disk with format conversion. Best for PDFs, DOCX, XLSX, PPTX, CSV, HTML, and images. For plain text or source code, prefer read_source() which is faster and supports line ranges."""
    from markitdown import MarkItDown

    p = Path(path)
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
        logger.info(f"read_file: {path} ({len(text)} chars)")
        return text[:20000]
    except Exception as e:
        logger.error(f"read_file failed: {path} → {e}", exc_info=True)
        return f"Error reading {path}: {e}"


@function_tool
def write_file(path: str, content: str) -> str:
    """Write content to a file. For modifying existing files, prefer patch_file() which does targeted find-and-replace. For handler source files, prefer write_core_file() which creates a git checkpoint first."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(content)
        logger.info(f"write_file: {path} ({len(content)} chars)")
        return f"Written {len(content)} chars to {path}"
    except Exception as e:
        logger.error(f"write_file failed: {path} → {e}", exc_info=True)
        return f"Error writing {path}: {e}"


@function_tool
def write_and_run(path: str, content: str, command: str) -> str:
    """Write a file and execute a shell command in one step. Use for writing scripts, configs, or data files that need immediate execution.

    Args:
        path:    File path to write to.
        content: File content to write.
        command: Shell command to run after writing (e.g. "python script.py", "bash setup.sh").
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(content)
        logger.info(f"write_and_run: wrote {path} ({len(content)} chars)")
    except Exception as e:
        logger.error(f"write_and_run: write failed: {path} → {e}")
        return f"Error writing {path}: {e}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
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
            f"write_and_run: {command} exit={result.returncode}, output={len(output)} chars"
        )
        return f"Wrote {len(content)} chars to {path}, then ran: {command}\n\n{output[:18000]}"
    except subprocess.TimeoutExpired:
        logger.error(f"write_and_run: command timed out: {command}")
        return f"Wrote {path}, but command timed out after 60 seconds: {command}"
    except Exception as e:
        logger.error(f"write_and_run: command failed: {command} → {e}")
        return f"Wrote {path}, but command failed: {e}"


@function_tool
def run_python(code: str) -> str:
    """Execute Python code inline and return the output. Print results to see them."""
    logger.info(f"run_python: {len(code)} chars")
    old_stdout = sys.stdout
    sys.stdout = captured = io.StringIO()
    try:
        exec(code, {"__builtins__": __builtins__})
        output = captured.getvalue()
        if not output:
            output = "(no output — use print() to see results)"
        logger.info(f"run_python: success ({len(output)} chars output)")
        return output[:20000]
    except Exception as e:
        logger.error(f"run_python failed: {e}")
        return f"Error: {e}"
    finally:
        sys.stdout = old_stdout


@function_tool
def run_shell(command: str) -> str:
    """Execute a shell command and return stdout + stderr."""
    logger.info(f"run_shell: {command}")
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
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
        logger.info(f"run_shell: exit={result.returncode}, output={len(output)} chars")
        return output[:20000]
    except subprocess.TimeoutExpired:
        logger.error(f"run_shell: timed out after 60s: {command}")
        return "Error: command timed out after 60 seconds"
    except Exception as e:
        logger.error(f"run_shell failed: {command} → {e}")
        return f"Error: {e}"


web_search = WebSearchTool()


def memory_tools(memory) -> list:
    """Return tools for reading/writing agent memory files."""

    @function_tool
    def remember(filename: str, content: str, append: bool = False) -> str:
        """Write or update a memory file (.md). Use to save information across conversations.

        Args:
            filename: Descriptive name ending in .md (e.g. 'user.md', 'project_abc.md').
            content:  The text to write.
            append:   If true, append content to the existing file instead of replacing it.
        """
        if append:
            existing = memory.read(filename) or ""
            content = existing + "\n" + content if existing else content
        memory.write(filename, content)
        mode = "appended to" if append else "wrote"
        logger.info(f"remember: {mode} {filename}")
        return f"Saved to memory: {filename}"

    @function_tool
    def recall_memory(filename: str) -> str:
        """Read the full content of a memory file. Use when the memory index is in context but you need the complete file."""
        content = memory.read(filename)
        if not content:
            return f"Memory file not found: {filename}"
        logger.info(f"recall_memory: read {filename} ({len(content)} chars)")
        return content

    @function_tool
    def forget(filename: str) -> str:
        """Delete a memory file permanently."""
        deleted = memory.delete(filename)
        if deleted:
            logger.info(f"forget: deleted {filename}")
            return f"Deleted memory file: {filename}"
        return f"Memory file not found: {filename}"

    @function_tool
    def list_memory() -> str:
        """List all memory files with their one-line summaries."""
        files = memory.list_files()
        if not files:
            return "No memory files yet."
        lines = [f"{f['filename']} ({f['size']} bytes) — {f['summary']}" for f in files]
        return "\n".join(lines)

    return [remember, recall_memory, forget, list_memory]
