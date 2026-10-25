from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from handler.runtime.harness.base import HarnessError, McpServerSpec, RunEventKind, RunLimits, RunRequest
from handler.runtime.harness.codex import SHELL_ENV_EXCLUDE, CodexHarness, _toml, codex_binary
from handler.runtime.types import TokenUsage

FIXTURES = Path(__file__).parents[1] / "fixtures" / "codex"
SESSION_ID = "01a14b0b-f792-7693-8670-4c8b06271552"


@pytest.fixture
def fake_codex(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['RECORD']).write_text(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}))\n"
        "Path(os.environ['RECORD'] + '.pid').write_text(str(os.getpid()))\n"
        "if os.environ.get('IGNORE_TERM'):\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "if os.environ.get('CHILD_PID'):\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "    Path(os.environ['CHILD_PID']).write_text(str(child.pid))\n"
        "if os.environ.get('STDERR'):\n"
        "    sys.stderr.write(os.environ['STDERR'])\n"
        "    sys.stderr.flush()\n"
        "if os.environ.get('STDERR_BYTES'):\n"
        "    sys.stderr.write('x' * int(os.environ['STDERR_BYTES']) + 'STDERR_END')\n"
        "    sys.stderr.flush()\n"
        "sys.stdout.buffer.write(Path(os.environ['FIXTURE']).read_bytes())\n"
        "sys.stdout.buffer.flush()\n"
        "if os.environ.get('CLOSE_STDOUT'):\n"
        "    os.close(sys.stdout.fileno())\n"
        "if os.environ.get('HANG'):\n"
        "    time.sleep(60)\n"
        "sys.exit(int(os.environ.get('EXIT_CODE', '0')))\n"
    )
    binary.chmod(0o755)
    return binary


@pytest.fixture
def request_for(tmp_path):
    def make(**kwargs):
        defaults = dict(
            run_id="run_test", thread_key="chat:user:1", workspace=tmp_path / "workspace",
            home=tmp_path / "home", prompt="Please do the work",
            env={"RECORD": str(tmp_path / "record.json"), "FIXTURE": str(FIXTURES / "exec_new_session.jsonl")},
        )
        defaults.update(kwargs)
        return RunRequest(**defaults)
    return make


@pytest.mark.parametrize(
    "fixture,session_id,usage,answer,kinds",
    [
        ("exec_new_session.jsonl", None, TokenUsage(30066, 26496, 0, 47, 0), "done",
         ["session", "turn_started", "tool_started", "tool_finished", "assistant_text", "usage", "final"]),
        ("exec_resume.jsonl", SESSION_ID, TokenUsage(45158, 41344, 0, 52, 0), "hi",
         ["session", "turn_started", "assistant_text", "usage", "final"]),
    ],
)
async def test_fixture_events(fake_codex, request_for, fixture, session_id, usage, answer, kinds):
    req = request_for(session_id=session_id)
    req.env["FIXTURE"] = str(FIXTURES / fixture)
    events = [ev async for ev in CodexHarness(fake_codex, default_model="test-model").run(req)]
    raw = [json.loads(line) for line in (FIXTURES / fixture).read_text().splitlines()]
    assert [ev.kind for ev in events] == kinds
    assert [ev.raw for ev in events[:-1]] == raw
    assert events[0].data == {"session_id": SESSION_ID}
    assert events[-2].usage == usage
    assert events[-2].data == {"model": "test-model"}
    assert events[-1].data == {"text": answer, "session_id": SESSION_ID}
    assert events[-1].raw == raw[-1]
    if session_id is None:
        assert events[2].data == {
            "id": "item_0", "tool": "shell", "kind": "command_execution",
            "input": raw[2]["item"]["command"],
        }
        assert events[3].data == {
            "id": "item_0", "tool": "shell", "kind": "command_execution",
            "input": raw[2]["item"]["command"], "output": "hi",
            "status": "completed", "exit_code": 0,
        }


@pytest.mark.parametrize("session_id", [None, SESSION_ID])
async def test_argv_and_environment(fake_codex, request_for, tmp_path, monkeypatch, session_id):
    monkeypatch.setenv("PARENT_VALUE", "inherited")
    auth = tmp_path / "source_auth.json"
    auth.write_text('{"token":"secret"}')
    req = request_for(
        session_id=session_id, model="requested-model", reasoning_effort="high",
        instructions="Use the workspace instructions.\n",
    )
    req.env["REQUEST_VALUE"] = "merged"
    harness = CodexHarness(
        fake_codex, default_model="default-model", default_reasoning_effort="low",
        sandbox="read-only", auth_json=auth, extra_args=["--color", "never"],
    )
    assert [ev async for ev in harness.run(req)][-1].kind == RunEventKind.FINAL
    recorded = json.loads((tmp_path / "record.json").read_text())
    expected = [
        "exec", "--json", "--ignore-user-config", "--skip-git-repo-check", "-s", "read-only",
        "-C", str(req.workspace), "-c", 'approval_policy="never"', *SHELL_POLICY_ARGS,
        "-m", "requested-model", "-c", "model_reasoning_effort=high",
        "--color", "never",
    ]
    if session_id:
        expected += ["resume", session_id]
    assert recorded["argv"] == expected + [req.prompt]
    assert recorded["env"]["CODEX_HOME"] == str(req.home)
    assert "PARENT_VALUE" not in recorded["env"]  # only allowlisted vars are inherited
    assert recorded["env"]["REQUEST_VALUE"] == "merged"
    copied = req.home / "auth.json"
    assert copied.read_bytes() == auth.read_bytes()
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600
    agents_md = (req.workspace / "AGENTS.md").read_text()
    assert agents_md.startswith(req.instructions)
    assert "handler-read" in agents_md and "view_image" in agents_md


async def test_existing_auth_and_instructions_preserved(fake_codex, request_for, tmp_path):
    req = request_for()
    req.home.mkdir()
    req.workspace.mkdir()
    auth = req.home / "auth.json"
    auth.write_text("existing credentials")
    instructions = req.workspace / "AGENTS.md"
    instructions.write_text("existing instructions")
    harness = CodexHarness(fake_codex, auth_json=tmp_path / "nonexistent_auth.json")
    assert [ev async for ev in harness.run(req)][-1].kind == RunEventKind.FINAL
    assert auth.read_text() == "existing credentials"
    assert instructions.read_text() == "existing instructions"


async def test_missing_auth_source_does_not_leave_empty_credentials(fake_codex, request_for, tmp_path):
    req = request_for()
    harness = CodexHarness(fake_codex, auth_json=tmp_path / "nonexistent_auth.json")
    with pytest.raises(HarnessError):
        async for _ in harness.run(req):
            pass
    assert not (req.home / "auth.json").exists()


def test_default_and_minimal_argv(request_for):
    req = request_for()
    base = [
        "codex-test", "exec", "--json", "--ignore-user-config", "--skip-git-repo-check",
        "-s", "workspace-write", "-C", str(req.workspace), "-c", 'approval_policy="never"',
        *SHELL_POLICY_ARGS,
    ]
    assert CodexHarness("codex-test")._argv(req) == base + [req.prompt]
    assert CodexHarness("codex-test", "model-default", "medium")._argv(req) == base + [
        "-m", "model-default", "-c", "model_reasoning_effort=medium", req.prompt,
    ]


def test_mcp_argv_toml_roundtrip(request_for):
    servers = [
        McpServerSpec(
            name='remote.server"雪', url='https://example.test/mcp?query="雪"',
            headers={"Authorization": 'Bearer "quoted"'},
        ),
        McpServerSpec(
            name="local", command=["/tools/my server", 'arg"one', "two\\three", "line\nbreak", "雪"],
            env={"TOKEN": 'a"b\\c', "A.B": "\tvalue"},
        ),
        McpServerSpec(name="empty", command=["server"]),
    ]
    req = request_for(session_id=SESSION_ID, mcp_servers=servers)
    argv = CodexHarness("codex-test", extra_args=["--color", "never"])._argv(req)
    overrides = [argv[i + 1] for i, arg in enumerate(argv)
                 if arg == "-c" and argv[i + 1].startswith("mcp_servers.")]
    parsed = {}
    for override in overrides:
        result = tomllib.loads(override)["mcp_servers"]
        for name, settings in result.items():
            parsed.setdefault(name, {}).update(settings)
        assert argv.index(override) < argv.index("--color") < argv.index("resume")
    assert parsed == {
        servers[0].name: {"url": servers[0].url, "http_headers": servers[0].headers,
                          "default_tools_approval_mode": "approve"},
        "local": {"command": servers[1].command[0], "args": servers[1].command[1:],
                  "env": servers[1].env, "default_tools_approval_mode": "approve"},
        "empty": {"command": "server", "args": [], "env": {},
                  "default_tools_approval_mode": "approve"},
    }


def test_codex_binary_env_takes_precedence(monkeypatch):
    monkeypatch.setenv("HANDLER_CODEX_BIN", "/custom/codex")
    monkeypatch.setattr("handler.runtime.harness.codex.shutil.which", lambda name: "/path/codex")
    assert codex_binary() == "/custom/codex"


def test_codex_binary_path_lookup_and_missing(monkeypatch):
    monkeypatch.delenv("HANDLER_CODEX_BIN", raising=False)
    monkeypatch.setattr("handler.runtime.harness.codex.shutil.which", lambda name: "/path/codex")
    assert codex_binary() == "/path/codex"
    monkeypatch.setattr("handler.runtime.harness.codex.shutil.which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="HANDLER_CODEX_BIN"):
        codex_binary()


def custom_output(req, tmp_path, objects):
    fixture = tmp_path / "custom.jsonl"
    fixture.write_text("".join(json.dumps(obj) + "\n" for obj in objects))
    req.env["FIXTURE"] = str(fixture)
    return req


async def test_reasoning_other_tools_and_usage(fake_codex, request_for, tmp_path):
    items = [
        {"id": "mcp1", "type": "mcp_tool_call", "server": "gateway", "tool": "lookup", "arguments": {"q": "x"}},
        {"id": "file1", "type": "file_change", "changes": [{"path": "file.txt", "kind": "add"}]},
        {"id": "web1", "type": "web_search", "query": "test"},
    ]
    raw = [{"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "Thinking"}}]
    for item in items:
        raw.append({"type": "item.started", "item": {**item, "status": "in_progress"}})
        raw.append({"type": "item.completed", "item": {**item, "status": "completed", "result": {"ok": True}}})
    raw += [
        {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "earlier"}},
        {"type": "item.completed", "item": {"id": "b", "type": "agent_message", "text": "latest"}},
        {"type": "turn.completed", "model": "reported-model", "usage": {
            "input_tokens": 17, "cached_input_tokens": 5, "cache_write_input_tokens": 2,
            "output_tokens": 9, "reasoning_output_tokens": 4,
        }},
    ]
    req = custom_output(request_for(session_id="resumed-session"), tmp_path, raw)
    events = [ev async for ev in CodexHarness(fake_codex, default_model="fallback").run(req)]
    assert [ev.raw for ev in events[:-1]] == raw
    assert events[0].kind == RunEventKind.REASONING
    assert events[0].data == {"text": "Thinking"}
    for index, tool in enumerate(["gateway/lookup", "file_change", "web_search"]):
        start, finish = events[1 + 2 * index:3 + 2 * index]
        assert start.kind == RunEventKind.TOOL_STARTED
        assert start.data == {
            "id": items[index]["id"], "tool": tool, "kind": items[index]["type"],
            "input": {k: v for k, v in items[index].items() if k not in {"id", "type"}},
        }
        assert finish.kind == RunEventKind.TOOL_FINISHED
        assert finish.data == {
            "id": items[index]["id"], "tool": tool, "kind": items[index]["type"],
            "input": {k: v for k, v in items[index].items() if k not in {"id", "type"}},
            "output": {"ok": True}, "status": "completed", "exit_code": None,
        }
    assert events[-2].usage == TokenUsage(17, 5, 2, 9, 4)
    assert events[-2].data == {"model": "reported-model"}
    assert events[-1].data == {"text": "latest", "session_id": "resumed-session"}


async def test_malformed_and_unknown_lines_skipped(fake_codex, request_for, tmp_path, caplog):
    fixture = tmp_path / "mixed.jsonl"
    fixture.write_bytes(
        b'not json\n\xff\n[]\nnull\n{"type":"future.event"}\n'
        b'{"type":["unknown"]}\n'
        b'{"type":"item.completed","item":{"type":"future.item"}}\n'
        b'{"type":"item.completed","item":{"type":["unknown"]}}\n'
        b'{"type":"item.completed"}\n'
        b'{"type":"turn.completed","usage":{}}'
    )
    req = request_for()
    req.env["FIXTURE"] = str(fixture)
    with caplog.at_level("DEBUG", logger="handler.runtime.harness.codex"):
        events = [ev async for ev in CodexHarness(fake_codex).run(req)]
    assert [ev.kind for ev in events] == [RunEventKind.USAGE, RunEventKind.FINAL]
    assert events[0].usage == TokenUsage()
    assert events[-1].data == {"text": "", "session_id": None}
    assert "Skipping malformed" in caplog.text
    assert "Skipping unknown" in caplog.text


async def test_large_tool_output_line(fake_codex, request_for, tmp_path):
    output = "雪" * 100000
    raw = [
        {"type": "item.completed", "item": {
            "id": "big", "type": "command_execution", "aggregated_output": output,
            "status": "completed", "exit_code": 0,
        }},
        {"type": "turn.completed", "usage": {}},
    ]
    req = custom_output(request_for(), tmp_path, raw)
    events = [ev async for ev in CodexHarness(fake_codex).run(req)]
    assert events[0].data["output"] == output
    assert events[0].raw == raw[0]
    assert events[-1].kind == RunEventKind.FINAL


@pytest.mark.parametrize("raw,retryable", [
    ({"type": "turn.failed", "error": {"message": "rate limited", "retryable": True}}, True),
    ({"type": "error", "message": "rate limited"}, False),
    ({"type": "error", "error": "rate limited"}, False),
])
async def test_error_event_then_raise(fake_codex, request_for, tmp_path, raw, retryable):
    req = custom_output(request_for(), tmp_path, [raw])
    req.env["HANG"] = "1"
    harness = CodexHarness(fake_codex)
    events = []
    with pytest.raises(HarnessError, match="rate limited") as caught:
        async for ev in harness.run(req):
            events.append(ev)
    assert caught.value.retryable is retryable
    assert len(events) == 1
    assert events[0].kind == RunEventKind.ERROR
    assert events[0].data == {"message": "rate limited", "retryable": retryable}
    assert events[0].raw == raw
    assert not harness._processes
    assert_dead(int(Path(req.env["RECORD"] + ".pid").read_text()))


@pytest.mark.parametrize("exit_code,completed", [(3, True), (0, False)])
async def test_exit_failures_include_bounded_stderr(fake_codex, request_for, tmp_path, exit_code, completed):
    req = request_for()
    if not completed:
        custom_output(req, tmp_path, [{"type": "turn.started"}])
    req.env.update(EXIT_CODE=str(exit_code), STDERR_BYTES="100000")
    events = []
    reason = "status 3" if exit_code else "without turn.completed"
    with pytest.raises(HarnessError, match=reason) as caught:
        async for ev in CodexHarness(fake_codex).run(req):
            events.append(ev)
    assert "STDERR_END" in str(caught.value)
    assert len(str(caught.value)) < 4300
    assert not caught.value.retryable
    assert events[-1].kind == RunEventKind.ERROR
    assert all(ev.kind != RunEventKind.FINAL for ev in events)


async def test_spawn_failure(request_for, tmp_path):
    events = []
    with pytest.raises(HarnessError, match="Could not start Codex"):
        async for ev in CodexHarness(tmp_path / "missing-codex").run(request_for()):
            events.append(ev)
    assert [ev.kind for ev in events] == [RunEventKind.ERROR]


def assert_dead(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def wait_for_file(filename):
    async with asyncio.timeout(3):
        while not filename.exists():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("close_stdout", [False, True])
async def test_timeout_kills_process(fake_codex, request_for, tmp_path, close_stdout):
    req = request_for(limits=RunLimits(timeout_seconds=0.4))
    req.env.update(HANG="1", STDERR="timeout details")
    if close_stdout:
        req.env["CLOSE_STDOUT"] = "1"
    harness = CodexHarness(fake_codex)
    events = []
    with pytest.raises(HarnessError, match="timed out") as caught:
        async for ev in harness.run(req):
            events.append(ev)
    assert caught.value.retryable
    assert "timeout details" in str(caught.value)
    assert events[-1].kind == RunEventKind.ERROR
    assert events[-1].data["retryable"] is True
    assert not harness._processes
    assert_dead(int((tmp_path / "record.json.pid").read_text()))


@pytest.mark.parametrize("parent_exits", [False, True])
async def test_cancellation_kills_process_group(fake_codex, request_for, tmp_path, parent_exits):
    req = request_for()
    child_pid = tmp_path / "child.pid"
    req.env.update(HANG="1", CHILD_PID=str(child_pid))
    if parent_exits:
        req.env.pop("HANG")
    harness = CodexHarness(fake_codex)

    async def consume():
        return [ev async for ev in harness.run(req)]

    task = asyncio.create_task(consume())
    try:
        await wait_for_file(child_pid)
        parent = int((tmp_path / "record.json.pid").read_text())
        child = int(child_pid.read_text())
        if parent_exits:
            async with asyncio.timeout(3):
                while harness._processes[req.run_id].returncode is None:
                    await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_dead(parent)
        async with asyncio.timeout(3):
            while True:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
        assert not harness._processes
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_interrupt_stops_tracked_run(fake_codex, request_for, tmp_path):
    req = request_for()
    req.env["HANG"] = "1"
    harness = CodexHarness(fake_codex)
    events = []

    async def consume():
        async for ev in harness.run(req):
            events.append(ev)

    task = asyncio.create_task(consume())
    try:
        await wait_for_file(tmp_path / "record.json.pid")
        await harness.interrupt("unknown-run")
        assert not task.done()
        await harness.interrupt(req.run_id)
        with pytest.raises(HarnessError, match="status"):
            await task
        assert_dead(int((tmp_path / "record.json.pid").read_text()))
        assert not harness._processes
        assert events[-1].kind == RunEventKind.ERROR
        await harness.interrupt(req.run_id)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_interrupt_leaves_other_runs_running(fake_codex, request_for, tmp_path):
    first = request_for(run_id="first")
    second = request_for(run_id="second", home=tmp_path / "other-home", workspace=tmp_path / "other-workspace")
    first.env["HANG"] = "1"
    second.env.update(HANG="1", RECORD=str(tmp_path / "other-record.json"))
    harness = CodexHarness(fake_codex)

    async def consume(req):
        return [ev async for ev in harness.run(req)]

    tasks = [asyncio.create_task(consume(req)) for req in (first, second)]
    try:
        await wait_for_file(tmp_path / "record.json.pid")
        await wait_for_file(tmp_path / "other-record.json.pid")
        await harness.interrupt(first.run_id)
        with pytest.raises(HarnessError):
            await tasks[0]
        assert not tasks[1].done()
        assert set(harness._processes) == {second.run_id}
        os.kill(int((tmp_path / "other-record.json.pid").read_text()), 0)
        tasks[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[1]
        assert not harness._processes
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_sigkill_after_term_ignored(fake_codex, request_for, tmp_path):
    req = request_for(limits=RunLimits(timeout_seconds=0.4))
    req.env.update(HANG="1", IGNORE_TERM="1")
    with pytest.raises(HarnessError, match="timed out"):
        async for _ in CodexHarness(fake_codex).run(req):
            pass
    assert_dead(int((tmp_path / "record.json.pid").read_text()))


@pytest.mark.skipif(os.environ.get("HANDLER_CODEX_LIVE") != "1", reason="Set HANDLER_CODEX_LIVE=1 for live Codex")
async def test_live_codex_new_and_resume(tmp_path):
    auth_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    auth = auth_home / "auth.json"
    harness = CodexHarness(codex_binary(), auth_json=auth if auth.exists() else None)
    req = RunRequest(
        run_id="live-new", thread_key="live", workspace=tmp_path / "workspace", home=tmp_path / "home",
        prompt="Reply with exactly 'hello' and do not use any tools.", limits=RunLimits(timeout_seconds=120),
    )
    events = [ev async for ev in harness.run(req)]
    assert events[-1].kind == RunEventKind.FINAL
    assert events[-1].data["session_id"]
    assert events[-1].data["text"]
    resumed = replace(
        req, run_id="live-resume", session_id=events[-1].data["session_id"],
        prompt="Repeat your previous response without using tools.",
    )
    events2 = [ev async for ev in harness.run(resumed)]
    assert events2[-1].kind == RunEventKind.FINAL
    assert events2[-1].data["session_id"] == events[-1].data["session_id"]
    assert events2[-1].data["text"]


async def test_child_stdin_is_closed(tmp_path):
    """Regression: an inherited stdin pipe stalled real codex runs."""
    import sys

    from handler.runtime.harness.base import RunRequest
    from handler.runtime.harness.codex import CodexHarness

    fixture = (
        '{"type":"thread.started","thread_id":"s1"}\n'
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
    )
    script = tmp_path / "codex"
    script.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "data = sys.stdin.read()  # blocks forever unless stdin is closed\n"
        f"sys.stdout.write({fixture!r})\n"
    )
    script.chmod(0o755)
    req = RunRequest(run_id="r", thread_key="t", workspace=tmp_path / "ws",
                     home=tmp_path / "home", prompt="hi")
    events = [ev async for ev in CodexHarness(script).run(req)]
    assert events[-1].data["session_id"] == "s1"


SHELL_POLICY_ARGS = [
    "-c", 'shell_environment_policy.inherit="core"',
    "-c", "shell_environment_policy.exclude=" + _toml(list(SHELL_ENV_EXCLUDE)),
]


async def test_environment_is_an_allowlist(fake_codex, request_for, tmp_path, monkeypatch):
    """Secrets in handler's environment never reach Codex (or the commands it runs)."""
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db/prod")
    monkeypatch.setenv("HANDLER_ADMIN_KEY", "admin-secret")
    monkeypatch.setenv("BUCKETEER_AWS_SECRET_ACCESS_KEY", "s3-secret")
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex")
    monkeypatch.setenv("TZ", "UTC")
    req = request_for()
    harness = CodexHarness(fake_codex)
    [ev async for ev in harness.run(req)]
    env = json.loads(Path(req.env["RECORD"]).read_text())["env"]
    assert "DATABASE_URL" not in env and "HANDLER_ADMIN_KEY" not in env
    assert "BUCKETEER_AWS_SECRET_ACCESS_KEY" not in env
    assert env["CODEX_API_KEY"] == "sk-codex"  # Codex itself needs it
    assert env["TZ"] == "UTC" and env["HOME"] == str(req.home) == env["CODEX_HOME"]


def test_sandbox_mode_is_validated_and_explicit(request_for):
    with pytest.raises(ValueError):
        CodexHarness("codex-test", sandbox="off")
    argv = CodexHarness("codex-test", sandbox="danger-full-access")._argv(request_for())
    assert argv[argv.index("-s") + 1] == "danger-full-access"
    assert "shell_environment_policy.inherit=\"core\"" in argv


def test_auth_json_link_mode(request_for, tmp_path):
    source = tmp_path / "login" / "auth.json"
    source.parent.mkdir()
    source.write_text('{"token":"a"}')
    req = request_for()
    harness = CodexHarness("codex-test", auth_json=source, auth_json_link=True)
    harness._prepare(req)
    target = req.home / "auth.json"
    assert target.is_symlink() and target.resolve() == source.resolve()
    source.write_text('{"token":"refreshed"}')  # a refresh by any run is shared
    assert target.read_text() == '{"token":"refreshed"}'
    # A copy left by copy mode (or a stale link) is replaced by the link.
    target.unlink()
    target.write_text("stale copy")
    harness._prepare(req)
    assert target.is_symlink() and target.read_text() == '{"token":"refreshed"}'


CATALOG = {"models": [
    {"slug": "hidden-top", "priority": 0, "visibility": "hide", "default_reasoning_level": "max"},
    {"slug": "second", "priority": 3, "visibility": "list", "default_reasoning_level": "high"},
    {"slug": "first", "priority": 1, "visibility": "list", "default_reasoning_level": "medium"},
]}


def catalog_codex(tmp_path, payload, exit_code=0):
    binary = tmp_path / "codex-catalog"
    calls = tmp_path / "calls"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.stdout.write({json.dumps(payload)!r})\n"
        f"sys.exit({exit_code})\n")
    binary.chmod(0o755)
    return binary, calls


async def test_defaults_from_catalog(tmp_path):
    binary, calls = catalog_codex(tmp_path, CATALOG)
    harness = CodexHarness(binary)
    assert await harness.defaults() == {"model": "first", "reasoning_effort": "medium"}
    assert await harness.defaults() == {"model": "first", "reasoning_effort": "medium"}
    assert calls.read_text().splitlines() == ["debug models"]  # cached


async def test_defaults_configured_skip_catalog_and_mix(tmp_path):
    binary, calls = catalog_codex(tmp_path, CATALOG)
    assert await CodexHarness(binary, default_model="m", default_reasoning_effort="low").defaults() \
        == {"model": "m", "reasoning_effort": "low"}
    assert not calls.exists()
    # A configured model takes its own catalog level.
    assert await CodexHarness(binary, default_model="second").defaults() == {
        "model": "second", "reasoning_effort": "high"}


async def test_defaults_unknown_when_catalog_fails(tmp_path):
    binary, calls = catalog_codex(tmp_path, {}, exit_code=1)
    harness = CodexHarness(binary)
    assert await harness.defaults() == {"model": None, "reasoning_effort": None}
    # Tried refreshed then bundled; the failure is not retried immediately.
    assert calls.read_text().splitlines() == ["debug models", "debug models --bundled"]
    await harness.defaults()
    assert len(calls.read_text().splitlines()) == 2
