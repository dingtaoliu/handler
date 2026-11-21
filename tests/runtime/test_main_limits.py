"""Caps on what the main process holds, the tenant scope, and per-run peak memory."""
import pytest

from handler.runtime import main_limits
from handler.runtime.context import TenantScope, current_tenant
from handler.runtime.observability.memory import RunPeak


def test_files_over_the_cap_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(main_limits, "max_file_bytes", 1024)
    small, large = tmp_path / "a.csv", tmp_path / "big.pdf"
    small.write_bytes(b"x" * 1024)
    large.write_bytes(b"x" * 1025)
    assert main_limits.read_bounded(small) == b"x" * 1024
    with pytest.raises(main_limits.FileTooLarge, match="big.pdf is 0.0 MB, over the 0 MB"):
        main_limits.read_bounded(large)


def test_caps_come_from_the_environment():
    try:
        main_limits.configure({"HANDLER_TOOL_RESULT_MAX_CHARS": "5000",
                               "HANDLER_MAIN_FILE_MAX_MB": "7"})
        assert (main_limits.max_tool_result_chars, main_limits.max_file_bytes) == (
            5000, 7 * 1024 * 1024)
        assert main_limits.cap_text("y" * 5000) == "y" * 5000
        assert main_limits.cap_text("y" * 5001).endswith(
            "[tool result truncated: 5,000 of 5,001 characters shown. "
            "Ask for less, e.g. fewer fields, rows or pages.]")
        with pytest.raises(ValueError):
            main_limits.configure({"HANDLER_TOOL_RESULT_MAX_CHARS": "10"})
    finally:
        main_limits.configure({})
    assert main_limits.max_tool_result_chars == 200_000


async def test_crontal_upload_over_the_file_cap_is_an_error(tmp_path, monkeypatch):
    from handler.runtime.context import RunContext
    from handler.runtime.gateway.crontal_api import CrontalApiTools

    monkeypatch.setattr(main_limits, "max_file_bytes", 10)
    (tmp_path / "big.xlsx").write_bytes(b"x" * 11)
    tools = CrontalApiTools("https://crontal.test", "agent-token", workspace_for=lambda _: tmp_path)
    ctx = RunContext(run_id="r", thread_key="chat:1", user_id="u", acting_as="u")
    result = await tools._request(ctx, "POST", {"path": "/api/v1/files",
                                                "upload_file": "big.xlsx"})
    assert result.is_error and "over the 0 MB limit" in result.content


async def test_tenant_scope_sets_the_tenant_for_each_request():
    seen = []

    async def app(scope, receive, send):
        seen.append(current_tenant())

    await TenantScope(app, "bss")({"type": "http"}, None, None)
    assert seen == ["bss"] and current_tenant() is None


async def test_run_peak_keeps_the_highest_sample(monkeypatch):
    from handler.runtime.observability import memory

    samples = iter([100.0, 180.0, 120.0])
    monkeypatch.setattr(memory, "process_memory", lambda: {"rss_mb": next(samples)})
    peak = RunPeak()
    peak.note()
    assert peak.finish() == 180.0
