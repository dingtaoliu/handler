"""Handler actions (tools) — split by concern."""

from .builtin import (
    read_file,
    write_file,
    write_and_run,
    run_python,
    run_shell,
    web_search,
    memory_tools,
)
from .selfmod import (
    mark_stable,
    restart_self,
    write_core_file,
)
from .session import compact_tool, compact_messages, cron_tools, stop_self
from .coding import search_codebase, patch_file, read_source
from .watchdog import get_health_problems

__all__ = [
    "read_file",
    "write_file",
    "write_and_run",
    "run_python",
    "run_shell",
    "web_search",
    "mark_stable",
    "restart_self",
    "write_core_file",
    "compact_tool",
    "compact_messages",
    "cron_tools",
    "stop_self",
    "get_health_problems",
    "memory_tools",
    "search_codebase",
    "patch_file",
    "read_source",
]
