import io
import json
import logging
from datetime import date, datetime, timezone

import pytest

from handler.runtime.context import RunContext, reset_current_run, set_current_run
from handler.runtime.harness.base import RunEvent, RunEventKind
from handler.runtime.observability.logging import configure_json_logging
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.observability.recorder import RawSink, StepRecorder
from handler.runtime.types import StepKind, TokenUsage

YAML = """
prices:
  - {model: "m-*", effective_from: 2025-01-01, input_per_mtok: 1, cached_input_per_mtok: 0.1, cache_write_per_mtok: 2, output_per_mtok: 10}
  - {model: "m-*", effective_from: 2026-01-01, input_per_mtok: 2, cached_input_per_mtok: 0.2, cache_write_per_mtok: 4, output_per_mtok: 20}
  - {model: "m-exact", effective_from: 2025-01-01, input_per_mtok: 3, cached_input_per_mtok: 0.3, cache_write_per_mtok: 6, output_per_mtok: 30}
  - {model: "nullrate", effective_from: 2025-01-01, input_per_mtok: 1, cached_input_per_mtok: null, output_per_mtok: 1}
"""


@pytest.fixture
def prices(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text(YAML)
    return PriceTable.from_yaml(p)


NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_cost_math_cached_split_and_cache_write(prices):
    u = TokenUsage(1_000_000, 400_000, 100_000, 50_000, 20_000)
    # 600k*2 + 400k*0.2 + 100k*4 + 50k*20 (reasoning not re-added), per Mtok
    assert prices.cost("m-new", u, NOW) == pytest.approx(1.2 + 0.08 + 0.4 + 1.0)


def test_exact_beats_glob_and_effective_dates(prices):
    u = TokenUsage(1_000_000)
    assert prices.cost("m-exact", u, NOW) == pytest.approx(3.0)
    assert prices.cost("m-x", u, NOW) == pytest.approx(2.0)
    assert prices.cost("m-x", u, date(2025, 6, 1)) == pytest.approx(1.0)
    assert prices.cost("m-x", u, date(2024, 1, 1)) is None


def test_unpriced_and_null_rates(prices):
    assert prices.cost("other", TokenUsage(10), NOW) is None
    assert prices.cost(None, TokenUsage(10), NOW) is None
    # null cached rate only matters when cached tokens are used
    assert prices.cost("nullrate", TokenUsage(1_000_000, 0, 0, 0), NOW) == pytest.approx(1.0)
    assert prices.cost("nullrate", TokenUsage(1_000_000, 5), NOW) is None


def test_shipped_prices_yaml_loads_unpriced():
    t = PriceTable.from_yaml()
    assert t.entries
    assert all(e.input_per_mtok is None for e in t.entries)
    assert t.cost("gpt-5.1", TokenUsage(100, 0, 0, 10), NOW) is None


class FakeStore:
    def __init__(self):
        self.steps = {}
        self.log = []

    async def add_step(self, step):
        self.steps[(step.run_id, step.seq)] = step
        self.log.append(("add", step.seq))

    async def update_step(self, step):
        assert (step.run_id, step.seq) in self.steps
        self.steps[(step.run_id, step.seq)] = step
        self.log.append(("update", step.seq))


def fixture_events(name, model="m-new"):
    """RunEvents as the codex harness would produce them from the fixtures."""
    ev = RunEvent
    K = RunEventKind
    if name == "new":
        cmd = "/bin/zsh -lc \"printf 'hi' > hello.txt && cat hello.txt\""
        return [
            ev(K.SESSION, {"session_id": "s1"}, raw={"type": "thread.started"}),
            ev(K.TURN_STARTED, raw={"type": "turn.started"}),
            ev(K.TOOL_STARTED, {"id": "item_0", "tool": "shell", "kind": "command_execution", "input": cmd}, raw={"type": "item.started"}),
            ev(K.TOOL_FINISHED, {"id": "item_0", "tool": "shell", "kind": "command_execution", "output": "hi", "status": "completed", "exit_code": 0}, raw={"type": "item.completed"}),
            ev(K.ASSISTANT_TEXT, {"text": "done"}, raw={"type": "item.completed"}),
            ev(K.USAGE, {"model": model}, usage=TokenUsage(30066, 26496, 0, 47, 0), raw={"type": "turn.completed"}),
            ev(K.FINAL, {"text": "done", "session_id": "s1"}),
        ]
    return [
        ev(K.SESSION, {"session_id": "s1"}),
        ev(K.TURN_STARTED),
        ev(K.ASSISTANT_TEXT, {"text": "hi"}, raw={"type": "item.completed"}),
        ev(K.USAGE, {"model": model}, usage=TokenUsage(45158, 41344, 0, 52, 0), raw={"type": "turn.completed"}),
        ev(K.FINAL, {"text": "hi", "session_id": "s1"}),
    ]


async def feed(rec, events):
    for e in events:
        await rec.on_event(e)


async def test_recorder_new_session(tmp_path, prices):
    store = FakeStore()
    rec = StepRecorder(store, "run_1", model="m-new", prices=prices, raw_sink=RawSink(tmp_path / "raw"))
    await feed(rec, fixture_events("new"))
    assert len(store.steps) == 3  # SESSION/TURN_STARTED/FINAL make no step
    assert [s.seq for s in store.steps.values()] == [1, 2, 3]
    tool = store.steps[("run_1", 1)]
    assert tool.kind == StepKind.TOOL_CALL and tool.name == "shell"
    assert tool.status == "ok" and tool.ended_at is not None
    assert tool.data["exit_code"] == 0 and tool.data["output"] == "hi"
    assert tool.data["output_size"] == 2
    assert store.log[:2] == [("add", 1), ("update", 1)]
    assert store.steps[("run_1", 2)].kind == StepKind.ASSISTANT_TEXT
    llm = store.steps[("run_1", 3)]
    assert llm.kind == StepKind.LLM_CALL and llm.model == "m-new"
    expected = (30066 - 26496) * 2 / 1e6 + 26496 * 0.2 / 1e6 + 47 * 20 / 1e6
    assert llm.cost_usd == pytest.approx(expected)
    usage, cost = rec.totals()
    assert usage.input_tokens == 30066 and cost == pytest.approx(expected)


async def test_recorder_raw_refs(tmp_path, prices):
    store = FakeStore()
    sink = RawSink(tmp_path / "raw")
    rec = StepRecorder(store, "run_1", model="m-new", prices=prices, raw_sink=sink)
    await feed(rec, fixture_events("new"))
    assert store.steps[("run_1", 1)].raw_ref == "run_1.jsonl#L1"
    assert store.steps[("run_1", 2)].raw_ref == "run_1.jsonl#L3"
    assert store.steps[("run_1", 3)].raw_ref == "run_1.jsonl#L4"
    lines = (tmp_path / "raw" / "run_1.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["type"] == "item.started"
    assert len(lines) == 4


async def test_resume_totals_and_unpriced(tmp_path, prices):
    store = FakeStore()
    rec = StepRecorder(store, "r2", model="m-new", prices=prices, raw_sink=RawSink(tmp_path))
    await feed(rec, fixture_events("resume") + fixture_events("resume"))
    usage, cost = rec.totals()
    assert usage.input_tokens == 90316 and usage.output_tokens == 104
    assert cost is not None
    rec2 = StepRecorder(FakeStore(), "r3", model="unknown", prices=prices, raw_sink=RawSink(tmp_path))
    await feed(rec2, fixture_events("resume", "unknown"))
    assert rec2.totals()[1] is None
    assert rec2.totals()[0].input_tokens == 45158


async def test_error_and_failed_tool_and_truncation(tmp_path, prices):
    store = FakeStore()
    rec = StepRecorder(store, "r", model=None, prices=prices, raw_sink=RawSink(tmp_path), preview_chars=10)
    K = RunEventKind
    await feed(rec, [
        RunEvent(K.TOOL_STARTED, {"id": "a", "tool": "t", "kind": "k", "input": "x" * 50}),
        RunEvent(K.TOOL_FINISHED, {"id": "a", "tool": "t", "kind": "k", "output": "y" * 50, "status": "failed", "exit_code": 1}),
        RunEvent(K.ERROR, {"message": "boom", "retryable": True}),
    ])
    t = store.steps[("r", 1)]
    assert t.status == "error" and t.data["input"] == "x" * 10 + "..."
    assert t.data["output_size"] == 50
    e = store.steps[("r", 2)]
    assert e.kind == StepKind.ERROR and e.data["retryable"] is True and e.raw_ref is None


def test_json_logging_includes_run_context():
    buf = io.StringIO()
    configure_json_logging("INFO", buf)
    log = logging.getLogger("t.obs")
    try:
        log.info("outside")
        tok = set_current_run(RunContext(run_id="run_9", thread_key="chat:u:c"))
        try:
            log.info("inside %s", 1)
            try:
                raise ValueError("x")
            except ValueError:
                log.exception("bad")
        finally:
            reset_current_run(tok)
    finally:
        for h in list(logging.getLogger().handlers):
            if getattr(h, "stream", None) is buf:
                logging.getLogger().removeHandler(h)
    rows = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert "run_id" not in rows[0] and rows[0]["msg"] == "outside"
    assert rows[1]["run_id"] == "run_9" and rows[1]["thread_key"] == "chat:u:c"
    assert rows[1]["msg"] == "inside 1" and {"ts", "level", "logger"} <= rows[1].keys()
    assert "ValueError" in rows[2]["exc_info"]
