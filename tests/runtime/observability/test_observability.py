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
SHIPPED_AT = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)  # prices.yaml rates verified


def test_cost_math_cached_split_and_cache_write(prices):
    u = TokenUsage(1_000_000, 400_000, 100_000, 50_000, 20_000)
    # input includes cache reads and writes: 500k uncached*2 + 400k cached*0.2
    # + 100k written*4 + 50k output*20 (reasoning not re-added), per Mtok
    assert prices.cost("m-new", u, NOW) == pytest.approx(1.0 + 0.08 + 0.4 + 1.0)


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


LONG = """
prices:
  - model: lc
    effective_from: 2025-01-01
    input_per_mtok: 2
    cached_input_per_mtok: 0.1
    cache_write_per_mtok: 2.5
    output_per_mtok: 10
    long_context:
      above_input_tokens: 272000
      input_per_mtok: 4
      cached_input_per_mtok: 0.2
      cache_write_per_mtok: 5
      output_per_mtok: 15
"""


@pytest.fixture
def long_prices(tmp_path):
    p = tmp_path / "long.yaml"
    p.write_text(LONG)
    return PriceTable.from_yaml(p)


def test_reasoning_tokens_billed_once_at_output_rate(prices):
    # reasoning is a subset of output: same cost with or without the breakdown
    assert prices.cost("m-new", TokenUsage(0, 0, 0, 1_000_000, 900_000), NOW) == pytest.approx(20.0)
    assert prices.cost("m-new", TokenUsage(0, 0, 0, 1_000_000, 0), NOW) == pytest.approx(20.0)


def test_cached_and_written_never_exceed_input(prices):
    # inconsistent counts are clamped, never billed below zero or twice
    assert prices.cost("m-new", TokenUsage(100_000, 500_000), NOW) == pytest.approx(0.02)
    assert prices.cost("m-new", TokenUsage(100_000, 60_000, 80_000), NOW) == pytest.approx(
        60_000 * 0.2 / 1e6 + 40_000 * 4 / 1e6)


def test_long_context_tier_is_per_request(long_prices):
    big = TokenUsage(300_000, 100_000, 50_000, 10_000, 4_000)
    short_cost = (150_000 * 2 + 100_000 * 0.1 + 50_000 * 2.5 + 10_000 * 10) / 1e6
    long_cost = (150_000 * 4 + 100_000 * 0.2 + 50_000 * 5 + 10_000 * 15) / 1e6
    assert long_prices.cost("lc", big, NOW, single_request=True) == pytest.approx(long_cost)
    # summed usage of several requests can't tell which crossed the threshold
    assert long_prices.cost("lc", big, NOW) == pytest.approx(short_cost)
    # the threshold itself is still short context
    at = TokenUsage(272_000)
    assert long_prices.cost("lc", at, NOW, single_request=True) == pytest.approx(0.544)
    assert long_prices.cost("lc", TokenUsage(272_001), NOW, single_request=True) == pytest.approx(
        272_001 * 4 / 1e6)


def test_long_context_needs_a_threshold(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("prices:\n  - {model: x, effective_from: 2025-01-01, long_context: {input_per_mtok: 1}}\n")
    with pytest.raises(ValueError):
        PriceTable.from_yaml(p)


def test_shipped_prices_cover_every_selectable_model():
    from handler.runtime.settings import HARNESS_DEFAULT_MODELS, ModelName

    t = PriceTable.from_yaml()
    models = {m.value for m in ModelName} | {m for m in HARNESS_DEFAULT_MODELS.values() if m}
    for model in models:
        entry = t.lookup(model, SHIPPED_AT)
        assert entry is not None, model
        for rates in (entry.rates, entry.long_context):
            assert rates is not None, model
            assert None not in (rates.input_per_mtok, rates.cached_input_per_mtok,
                                rates.cache_write_per_mtok, rates.output_per_mtok), model
            assert rates.cached_input_per_mtok < rates.input_per_mtok < rates.cache_write_per_mtok
        assert entry.long_context_above_tokens == 272_000
    assert t.cost("gpt-unknown", TokenUsage(100), SHIPPED_AT) is None


def test_shipped_gpt_6_1_sol_rates():
    # developers.openai.com/api/docs/pricing, 2026-10-18: $2 / $0.10 cached /
    # $2.50 cache write / $10 output; above 272K: $4 / $0.20 / $5 / $15.
    t = PriceTable.from_yaml()
    u = TokenUsage(1_000_000, 500_000, 100_000, 100_000, 60_000)
    assert t.cost("gpt-6.1-sol", u, SHIPPED_AT) == pytest.approx(0.8 + 0.05 + 0.25 + 1.0)
    assert t.cost("gpt-6.1-sol", u, SHIPPED_AT, single_request=True) == pytest.approx(
        1.6 + 0.1 + 0.5 + 1.5)
    assert t.cost("gpt-6.1-sol", u, date(2026, 1, 1)) is None  # before verification


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


async def test_recorder_prices_each_request_and_its_long_context(tmp_path, long_prices):
    store = FakeStore()
    rec = StepRecorder(store, "r", model="lc", prices=long_prices, raw_sink=RawSink(tmp_path))
    K = RunEventKind
    big, small = TokenUsage(300_000, 0, 0, 1_000), TokenUsage(100_000, 0, 0, 1_000)
    await feed(rec, [
        RunEvent(K.USAGE, {"model": "lc", "single_request": True}, usage=big, at=NOW),
        RunEvent(K.USAGE, {"model": "lc", "single_request": True}, usage=small, at=NOW),
        RunEvent(K.USAGE, {"model": "lc"}, usage=big, at=NOW),  # summed: standard rates
    ])
    costs = [store.steps[("r", i)].cost_usd for i in (1, 2, 3)]
    assert costs == pytest.approx([(300_000 * 4 + 1_000 * 15) / 1e6,
                                   (100_000 * 2 + 1_000 * 10) / 1e6,
                                   (300_000 * 2 + 1_000 * 10) / 1e6])
    usage, cost = rec.totals()
    assert usage.input_tokens == 700_000 and cost == pytest.approx(sum(costs))


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
