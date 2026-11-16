"""The reader pool: parsing in child processes, one host-wide cap shared fairly by
tenants, with timeouts, kills, recycling and memory logs."""
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

import pytest

from handler.runtime import documents
from handler.runtime.context import (
    RunContext,
    reset_current_run,
    reset_current_tenant,
    set_current_run,
    set_current_tenant,
)
from handler.runtime.documents import limiter
from handler.runtime.observability import memory
from handler.runtime.observability.logging import JsonFormatter
from tests.runtime import reader_fakes as fakes

SRC = Path(__file__).resolve().parents[2] / "src" / "handler" / "runtime"


@pytest.fixture(autouse=True)
def restore_pool():
    yield
    documents.configure_readers({})


def configure(**env):
    names = {"limit": "HANDLER_DOC_READ_MAX_CONCURRENT",
             "timeout": "HANDLER_READER_TIMEOUT_SECONDS", "memory": "HANDLER_READER_MEMORY_MB",
             "tasks": "HANDLER_READER_MAX_TASKS", "result": "HANDLER_READER_MAX_RESULT_MB",
             "idle": "HANDLER_READER_IDLE_SECONDS"}
    return documents.configure_readers({names[k]: str(v) for k, v in env.items()})


class Gate:
    """Coordinates gated reads in child processes through a directory."""

    def __init__(self, root: Path):
        self.root = root
        (root / "started").mkdir(parents=True)
        self.file = root / "doc.pdf"
        self.file.write_text("x")

    @property
    def started(self) -> int:
        return len(list((self.root / "started").iterdir()))

    def release(self):
        (self.root / "release").touch()

    def read(self, source="test"):
        return documents.capped_read(source, fakes.gated, self.file, str(self.root))


async def until(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


async def as_tenant(tenant, coroutine):
    token = set_current_tenant(tenant)
    try:
        return await coroutine
    finally:
        reset_current_tenant(token)


async def test_cap_holds_across_every_caller(tmp_path):
    configure(limit=2)
    gate = Gate(tmp_path)
    reads = [asyncio.create_task(gate.read(f"instance-{n}")) for n in range(6)]
    await until(lambda: gate.started == 2)
    await asyncio.sleep(0.2)
    assert gate.started == 2 and limiter.reader_pool().active == 2
    gate.release()
    assert await asyncio.gather(*reads) == ["doc.pdf"] * 6
    assert gate.started == 6


async def test_a_caller_giving_up_kills_its_read_and_frees_the_slot(tmp_path):
    configure(limit=1)
    gate = Gate(tmp_path)
    abandoned = asyncio.create_task(gate.read("email_intake"))
    await until(lambda: gate.started == 1)
    abandoned.cancel()  # like asyncio.wait_for timing out
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    # The child was killed, so the slot is free although the gate never opened.
    assert await documents.capped_read("crontal_api", fakes.text, gate.file) == "text"


async def test_a_queued_read_can_be_cancelled_without_running(tmp_path):
    configure(limit=1)
    gate = Gate(tmp_path)
    first = asyncio.create_task(gate.read("a"))
    await until(lambda: gate.started == 1)
    queued = asyncio.create_task(gate.read("b"))
    await asyncio.sleep(0.1)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    gate.release()
    await first
    assert gate.started == 1


async def test_a_hanging_read_is_killed_and_replaced_and_others_proceed(tmp_path):
    configure(limit=2, timeout=1)
    pool = limiter.reader_pool()
    file = tmp_path / "hung.pdf"
    file.write_text("x")
    hung = asyncio.create_task(as_tenant("a", documents.capped_read("read_file", fakes.hang, file)))
    await until(lambda: pool.active == 1)
    # Another tenant's reads run meanwhile on the other slot.
    other = [await as_tenant("b", documents.capped_read("read_file", fakes.text, file))
             for _ in range(3)]
    assert other == ["text"] * 3
    with pytest.raises(documents.ReaderFailed) as failed:
        await hung
    assert failed.value.outcome == "timeout"
    assert "hung.pdf took longer than 1s and was stopped" in str(failed.value)
    started = pool.started
    assert await documents.capped_read("read_file", fakes.text, file) == "text"
    assert pool.active == 0 and pool.started <= started + 1


async def test_a_crashed_child_fails_only_its_call(tmp_path):
    configure(limit=2)
    file = tmp_path / "bad.xlsx"
    file.write_text("x")
    crashed, ok = await asyncio.gather(
        documents.capped_read("read_file", fakes.crash, file),
        documents.capped_read("read_file", fakes.text, file), return_exceptions=True)
    assert ok == "text"
    assert isinstance(crashed, documents.ReaderFailed) and crashed.outcome == "crashed"
    assert "signal 9" in str(crashed)
    assert await documents.capped_read("read_file", fakes.text, file) == "text"


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced only on Linux")
async def test_a_read_over_the_memory_limit_fails_and_the_child_is_replaced(tmp_path):
    configure(limit=1, memory=1200)
    file = tmp_path / "huge.pdf"
    file.write_text("x")
    with pytest.raises(documents.ReaderFailed) as failed:
        await documents.capped_read("read_file", fakes.allocate, file, 2048)
    assert failed.value.outcome in ("memory", "crashed")
    assert await documents.capped_read("read_file", fakes.allocate, file, 16) == 16 * 1024 * 1024


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced only on Linux")
async def test_a_synthetic_oversized_pdf_kills_only_its_child(tmp_path):
    """The step 4 check: rendering a huge page exceeds the child's memory limit."""
    import fitz

    configure(limit=2, memory=1200)
    huge = tmp_path / "huge.pdf"
    with fitz.open() as doc:
        # A 9,000-point page renders to a ~570 MB pixmap at the reader's DPI.
        doc.new_page(width=9_000, height=9_000).insert_text((40, 40), "big")
        doc.save(huge)
    small = tmp_path / "notes.txt"
    small.write_text("fine")

    async def read(path, **options):
        return await documents.capped_read("read_file", documents.read, path, **options)

    oversized, other = await asyncio.gather(
        as_tenant("a", read(huge, pages="1", as_images=True)),
        as_tenant("b", read(small)), return_exceptions=True)
    assert isinstance(oversized, documents.ReaderFailed), oversized
    assert oversized.outcome == "memory" and "more memory than a reader may use" in str(oversized)
    assert other.text == "fine"
    assert (await read(small)).text == "fine"  # the replacement child serves the next read


async def test_an_oversized_result_is_refused_in_the_child(tmp_path):
    configure(result=1)
    file = tmp_path / "a.docx"
    file.write_text("x")
    with pytest.raises(documents.ReaderFailed) as failed:
        await documents.capped_read("read_file", fakes.big_result, file, 2)
    assert failed.value.outcome == "too_large"
    assert len(await documents.capped_read("read_file", fakes.big_result, file, 0)) == 0


async def test_children_are_recycled_after_their_task_count(tmp_path):
    configure(limit=1, tasks=2)
    file = tmp_path / "a.csv"
    file.write_text("x")
    pids = [await documents.capped_read("read_file", fakes.pid, file) for _ in range(5)]
    assert pids[0] == pids[1] != pids[2] == pids[3] != pids[4]


async def test_idle_children_stop(tmp_path):
    configure(limit=1, idle=0.2)
    pool = limiter.reader_pool()
    file = tmp_path / "a.csv"
    file.write_text("x")
    first = await documents.capped_read("read_file", fakes.pid, file)
    await until(lambda: not pool._idle, timeout=5)
    assert await documents.capped_read("read_file", fakes.pid, file) != first


async def test_one_tenant_cannot_hold_every_slot_while_another_waits(tmp_path, caplog):
    configure(limit=2)
    pool = limiter.reader_pool()
    gate = Gate(tmp_path)
    hogs = [asyncio.create_task(as_tenant("a", gate.read())) for _ in range(2)]
    await until(lambda: gate.started == 2)
    assert pool.holding("a") == 2  # alone, a tenant may use every slot
    with caplog.at_level(logging.WARNING, logger="handler.runtime.documents"):
        # b's read runs at once: a's newest read is stopped and queued again.
        other = await asyncio.wait_for(
            as_tenant("b", documents.capped_read("read_file", fakes.text, gate.file)), 10)
    assert other == "text"
    assert any(r.getMessage() == "document read stopped for another tenant"
               and r.tenant == "a" for r in caplog.records)
    await until(lambda: gate.started == 3)  # the stopped read started again
    assert all(not task.done() for task in hogs)
    gate.release()
    assert await asyncio.gather(*hogs) == ["doc.pdf"] * 2


async def test_a_tenant_waits_for_its_own_reads_without_stopping_them(tmp_path):
    configure(limit=2)
    gate = Gate(tmp_path)
    reads = [asyncio.create_task(as_tenant("a", gate.read())) for _ in range(3)]
    await until(lambda: gate.started == 2)
    await asyncio.sleep(0.3)
    assert gate.started == 2  # no preemption within one tenant
    gate.release()
    assert await asyncio.gather(*reads) == ["doc.pdf"] * 3
    assert gate.started == 3


async def test_a_free_slot_goes_to_the_tenant_holding_fewest(tmp_path):
    configure(limit=2)
    pool = limiter.reader_pool()
    gate_a, gate_b = Gate(tmp_path / "a"), Gate(tmp_path / "b")
    a1 = asyncio.create_task(as_tenant("a", gate_a.read()))
    b1 = asyncio.create_task(as_tenant("b", gate_b.read()))
    await until(lambda: gate_a.started == 1 and gate_b.started == 1)
    a2 = asyncio.create_task(as_tenant("a", gate_a.read()))  # queued first
    await asyncio.sleep(0.1)
    gate_c = Gate(tmp_path / "c")
    c = asyncio.create_task(as_tenant("c", gate_c.read()))
    await asyncio.sleep(0.1)
    gate_b.release()  # b's slot frees: c (holding none) goes before a (holding one)
    await until(lambda: gate_c.started == 1)
    assert gate_a.started == 1 and not a2.done()
    gate_a.release()
    gate_c.release()
    await asyncio.gather(a1, a2, b1, c)
    assert pool.active == 0


def test_bad_settings_are_rejected():
    for env in ({"HANDLER_DOC_READ_MAX_CONCURRENT": "0"},
                {"HANDLER_DOC_READ_MAX_CONCURRENT": "many"},
                {"HANDLER_READER_TIMEOUT_SECONDS": "0"},
                {"HANDLER_READER_MAX_TASKS": "0"},
                {"HANDLER_READER_MEMORY_MB": "-1"}):
        with pytest.raises(ValueError):
            documents.configure_readers(env)
    assert documents.configure_readers({"HANDLER_READER_MEMORY_MB": "0"}).memory_mb == 0


async def test_each_read_logs_memory_with_run_context_and_tenant(tmp_path, caplog):
    file = tmp_path / "Quote.PDF"
    file.write_bytes(b"12345")
    documents.pop_reader_peak("run_1")  # left by other tests' reads
    token = set_current_run(RunContext(run_id="run_1", thread_key="chat:1", user_id="u"))
    try:
        with caplog.at_level(logging.INFO, logger="handler.runtime.documents"):
            assert await as_tenant("bss", documents.capped_read(
                "read_file", fakes.text, file)) == "text"
            line = json.loads(JsonFormatter().format(caplog.records[-1]))
    finally:
        reset_current_run(token)
    assert line["msg"] == "document read" and line["outcome"] == "ok"
    assert (line["run_id"], line["thread_key"], line["tenant"]) == ("run_1", "chat:1", "bss")
    assert (line["source"], line["suffix"], line["size_bytes"]) == ("read_file", ".pdf", 5)
    assert line["peak_rss_mb"] > 0 and "rss_mb_before" in line and "rss_mb_after" in line
    assert line["reader_pid"] and "main_rss_mb" in line
    assert line["active_reads"] == 1 and line["queued_ms"] >= 0
    assert "Quote" not in json.dumps(line)  # customer file names stay out of logs
    assert documents.pop_reader_peak("run_1") == line["peak_rss_mb"]
    assert documents.pop_reader_peak("run_1") is None


async def test_a_failing_read_is_logged_and_raises(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="handler.runtime.documents"):
        with pytest.raises(documents.ReadError, match="corrupt"):
            await documents.capped_read("gmail", fakes.broken, tmp_path / "gone.pdf")
    assert caplog.records[-1].outcome == "error"
    assert caplog.records[-1].size_bytes is None


async def test_a_read_waiting_for_a_slot_is_logged_with_its_tenant(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(limiter, "SLOW_READ_SECONDS", 0.1)
    configure(limit=1)
    gate = Gate(tmp_path)
    with caplog.at_level(logging.WARNING, logger="handler.runtime.documents"):
        first = asyncio.create_task(as_tenant("a", gate.read("a")))
        await until(lambda: gate.started == 1)
        second = asyncio.create_task(as_tenant("a", gate.read("b")))
        await asyncio.sleep(0.4)
        gate.release()
        await asyncio.gather(first, second)
    waiting = [r for r in caplog.records if r.getMessage() == "document read waiting for a slot"]
    running = [r for r in caplog.records if r.getMessage() == "document read still running"]
    assert waiting and running
    assert waiting[0].tenant == "a" and waiting[0].limit == 1 and waiting[0].active_reads == 1


async def test_previews_report_a_stopped_reader_instead_of_raising(tmp_path, monkeypatch):
    configure(timeout=0.5)
    monkeypatch.setattr(documents.router, "to_text", fakes.hang)
    file = tmp_path / "a.docx"
    file.write_text("x")
    text, error = await documents.capped_preview("crontal_api", file, max_chars=100)
    assert text is None and "took longer than 0.5s" in error


async def test_email_bytes_are_parsed_in_a_reader(tmp_path, caplog):
    from email.message import EmailMessage

    from handler.runtime.integrations.mailbox.message import parse_message_file
    from handler.runtime.mail.parse import parse_email_file

    message = EmailMessage()
    message["From"], message["Subject"], message["Message-ID"] = "a@x.test", "Hi", "<m1@x>"
    message.set_content("body")
    message.add_attachment(b"col\n1\n", maintype="text", subtype="csv", filename="t.csv")
    raw = message.as_bytes()
    with caplog.at_level(logging.INFO, logger="handler.runtime.documents"):
        parsed = await documents.capped_parse("email_ingest", parse_email_file, raw)
        mail = await documents.capped_parse("mailbox_ingest", parse_message_file, raw,
                                            headers_only=True)
    assert parsed.message_id == "m1@x" and parsed.attachments[0].data == b"col\n1\n"
    assert mail.subject == "Hi" and mail.headers_only
    sources = [r.source for r in caplog.records if r.getMessage() == "document read"]
    assert sources == ["email_ingest", "mailbox_ingest"]
    assert not list(limiter.reader_pool().scratch_dir().iterdir())  # handed-over files removed


async def test_conversions_are_cached_across_reader_processes(tmp_path):
    configure(limit=1, tasks=1)  # every call gets a new child
    from handler.runtime.documents import cache

    doc = tmp_path / "page.html"
    doc.write_text("<p>" + "word " * 50 + "</p>")
    first = await documents.capped_read("read_file", documents.read, doc)
    second = await documents.capped_read("read_file", documents.read, doc)
    assert first.text == second.text
    cached = list((limiter.reader_pool()._root / "cache").glob("*.txt"))
    assert len(cached) == 1
    assert cache._dir is None  # the main process keeps its in-memory cache


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


def test_every_parse_goes_through_the_pool():
    direct = re.compile(
        r"to_thread\(\s*(documents\.(read|to_text)|prepare_\w+|parse_email|parse_message|downscale)"
        r"\b|[^_.\w]downscale\(|[^.\w]parse_email\(\s*body|[^.\w]parse_message\(\s*raw")
    offenders = [str(path.relative_to(SRC)) for path in SRC.rglob("*.py")
                 if path.parent.name != "documents" and path.name not in ("images.py",)
                 and direct.search(path.read_text().split("def parse_message(")[0]
                                   .split("def parse_email(")[0])]
    assert offenders == []
    for name, calls in {"gateway/crontal_api.py": "capped_preview(",
                        "gateway/gmail.py": "capped_preview(",
                        "integrations/mailbox/tools.py": "capped_preview(",
                        "mail/intake.py": "capped_preview(",
                        "harness/pdf_files.py": "capped_read(",
                        "run.py": "capped_read(",
                        "web/email_ingest.py": "capped_parse(",
                        "integrations/mailbox/service.py": "capped_parse("}.items():
        assert calls in (SRC / name).read_text(), name
    assert (SRC / "harness/openai_agents.py").read_text().count("capped_read(") == 2


def test_settings_are_read_from_the_host_environment(tmp_path):
    from handler.runtime import server

    env = {"HANDLER_DOC_READ_MAX_CONCURRENT": "5", "HANDLER_READER_TIMEOUT_SECONDS": "30",
           "HANDLER_RT_DATA_DIR": str(tmp_path)}
    server.create_host(server._parse_args(["serve"], env), env)
    assert limiter._limit == 5 and limiter.reader_pool().settings.timeout_seconds == 30
    env["HANDLER_DOC_READ_MAX_CONCURRENT"] = "zero"
    with pytest.raises(ValueError):
        server.create_host(server._parse_args(["serve"], env), env)


async def test_the_host_lifespan_stops_the_readers(tmp_path):
    import multiprocessing

    from handler.runtime import server

    env = {"HANDLER_RT_DATA_DIR": str(tmp_path), "HANDLER_ADMIN_KEY": "k",
           "OPENAI_API_KEY": "sk-test"}
    host = server.create_host(server._parse_args(["serve"], env), env)
    file = tmp_path / "a.txt"
    file.write_text("hi")
    async with host.router.lifespan_context(host):
        assert (await documents.capped_read("read_file", documents.read, file)).text == "hi"
        assert multiprocessing.active_children()
    assert multiprocessing.active_children() == [] and limiter._pool is None
