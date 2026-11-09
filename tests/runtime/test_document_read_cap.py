"""Document reads share one process-wide cap and log process memory."""
import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path

import pytest

from handler.runtime import documents
from handler.runtime.documents import limiter
from handler.runtime.context import RunContext, reset_current_run, set_current_run
from handler.runtime.observability import memory
from handler.runtime.observability.logging import JsonFormatter

SRC = Path(__file__).resolve().parents[2] / "src" / "handler" / "runtime"


@pytest.fixture(autouse=True)
def restore_limit():
    yield
    documents.configure_read_limit(None)


class Gate:
    """A fake reader that blocks until released and records the overlap."""

    def __init__(self):
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.running = self.peak = self.started = 0

    def __call__(self, path):
        with self.lock:
            self.running += 1
            self.started += 1
            self.peak = max(self.peak, self.running)
        try:
            assert self.release.wait(10)
            return path
        finally:
            with self.lock:
                self.running -= 1


async def until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


async def test_cap_holds_across_every_caller(tmp_path):
    documents.configure_read_limit("2")
    gate = Gate()
    file = tmp_path / "a.pdf"
    file.write_text("x")
    # Different sources stand in for different instances and read paths.
    reads = [asyncio.create_task(documents.capped_read(f"instance-{n}", gate, file))
             for n in range(8)]
    await until(lambda: gate.started == 2)
    await asyncio.sleep(0.1)
    assert (gate.running, gate.started) == (2, 2)
    gate.release.set()
    assert await asyncio.gather(*reads) == [file] * 8
    assert gate.peak == 2 and gate.started == 8


async def test_cap_counts_a_read_its_caller_gave_up_on(tmp_path):
    documents.configure_read_limit("1")
    gate = Gate()
    file = tmp_path / "a.xlsx"
    file.write_text("x")
    abandoned = asyncio.create_task(documents.capped_read("email_intake", gate, file))
    await until(lambda: gate.running == 1)
    abandoned.cancel()  # like asyncio.wait_for timing out: the thread keeps decoding
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    second = asyncio.create_task(documents.capped_read("crontal_api", gate, file))
    await asyncio.sleep(0.1)
    assert (gate.running, gate.started) == (1, 1)  # still blocked by the abandoned read
    gate.release.set()
    assert await second == file
    assert gate.peak == 1


async def test_a_queued_read_can_be_cancelled_without_running(tmp_path):
    documents.configure_read_limit("1")
    gate = Gate()
    file = tmp_path / "a.docx"
    file.write_text("x")
    first = asyncio.create_task(documents.capped_read("a", gate, file))
    await until(lambda: gate.running == 1)
    queued = asyncio.create_task(documents.capped_read("b", gate, file))
    await asyncio.sleep(0.05)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    gate.release.set()
    await first
    assert gate.started == 1


@pytest.mark.parametrize("bad", ["0", "-1", "many"])
def test_bad_limit_is_rejected(bad):
    with pytest.raises(ValueError):
        documents.configure_read_limit(bad)


async def test_each_read_logs_memory_with_run_context(tmp_path, caplog):
    file = tmp_path / "Quote.PDF"
    file.write_bytes(b"12345")
    token = set_current_run(RunContext(run_id="run_1", thread_key="chat:1", user_id="u"))
    try:
        with caplog.at_level(logging.INFO, logger="handler.runtime.documents"):
            assert await documents.capped_read("read_file", lambda path: "text", file) == "text"
            line = json.loads(JsonFormatter().format(caplog.records[-1]))
    finally:
        reset_current_run(token)
    assert line["msg"] == "document read" and line["outcome"] == "ok"
    assert (line["run_id"], line["thread_key"]) == ("run_1", "chat:1")
    assert (line["source"], line["suffix"], line["size_bytes"]) == ("read_file", ".pdf", 5)
    assert line["peak_rss_mb"] > 0 and "rss_mb_before" in line and "rss_mb_after" in line
    assert line["active_reads"] == 1 and line["queued_ms"] >= 0
    assert "Quote" not in json.dumps(line)  # customer file names stay out of logs


async def test_a_failing_read_is_logged_and_raises(tmp_path, caplog):
    def broken(path):
        raise documents.ReadError("corrupt")

    with caplog.at_level(logging.INFO, logger="handler.runtime.documents"):
        with pytest.raises(documents.ReadError):
            await documents.capped_read("gmail", broken, tmp_path / "gone.pdf")
    assert caplog.records[-1].outcome == "error"
    assert caplog.records[-1].size_bytes is None


def test_memory_reads_proc_status(tmp_path, monkeypatch):
    status = tmp_path / "status"
    status.write_text("Name: x\nVmHWM:\t  204800 kB\nVmRSS:\t  102400 kB\nVmSwap:\t    1024 kB\n")
    monkeypatch.setattr(memory, "_STATUS", status)
    assert memory.process_memory() == {"rss_mb": 100.0, "peak_rss_mb": 200.0, "swap_mb": 1.0}


def test_memory_falls_back_to_peak_without_proc(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "_STATUS", tmp_path / "missing")
    sample = memory.process_memory()
    assert sample["rss_mb"] is None and sample["swap_mb"] is None
    assert sample["peak_rss_mb"] > 0


def test_every_in_process_read_goes_through_the_cap():
    direct = re.compile(r"to_thread\(\s*(documents\.(read|to_text)|prepare_input)\b")
    offenders = [str(path.relative_to(SRC)) for path in SRC.rglob("*.py")
                 if path.parent.name != "documents" and direct.search(path.read_text())]
    assert offenders == []
    sources = {path.read_text().count("capped_read(") for path in (
        SRC / "gateway/crontal_api.py", SRC / "harness/pdf_files.py", SRC / "gateway/gmail.py",
        SRC / "integrations/mailbox/tools.py", SRC / "mail/intake.py")}
    assert 0 not in sources
    assert (SRC / "harness/openai_agents.py").read_text().count("capped_read(") == 2


def test_limit_is_read_from_the_host_environment(tmp_path):
    from handler.runtime import server

    env = {"HANDLER_DOC_READ_MAX_CONCURRENT": "5", "HANDLER_RT_DATA_DIR": str(tmp_path)}
    server.create_host(server._parse_args(["serve"], env), env)
    assert limiter._limit == 5
    env["HANDLER_DOC_READ_MAX_CONCURRENT"] = "zero"
    with pytest.raises(ValueError):
        server.create_host(server._parse_args(["serve"], env), env)
