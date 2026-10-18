import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from handler.runtime.bus import RunEventBus
from handler.runtime.files import LocalFileStore
from handler.runtime.gateway.tokens import RunTokens
from handler.runtime.harness.base import HarnessError, RunEvent, RunEventKind, RunRequest
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.run import RunConfig, RunExecutor, thread_slug
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import (
    Event, Lane, Message, Role, RunStatus, StepKind, ThreadSpec, ThreadType, TokenUsage, utcnow,
)
from handler.runtime.workspaces import WorkspaceSync

KEY = "chat:user:conversation"
OLD_SESSION = "lost-session"


class FakeHarness:
    name = "fake"

    def __init__(self, resume_error=None, fresh_error=None):
        self.requests: list[RunRequest] = []
        self.resume_error = resume_error
        self.fresh_error = fresh_error
        self.before_run = None

    async def run(self, req):
        self.requests.append(req)
        if self.before_run:
            self.before_run(req)
        (req.workspace / "note.txt").write_text("saved workspace")
        (req.home / "auth.json").write_text("secret")
        error = self.resume_error if req.session_id else self.fresh_error
        if error:
            yield RunEvent(RunEventKind.ERROR, {"message": str(error)})
            raise error
        yield RunEvent(RunEventKind.SESSION, {"session_id": "fresh-session"})
        yield RunEvent(RunEventKind.ASSISTANT_TEXT, {"text": "recovered reply"})
        yield RunEvent(RunEventKind.USAGE, {"model": "fake"}, usage=TokenUsage(input_tokens=10))
        yield RunEvent(RunEventKind.FINAL, {"text": "recovered reply"})

    async def interrupt(self, run_id):
        pass


@pytest.fixture
async def store(tmp_path):
    instance = SQLiteStore(tmp_path / "store.db")
    try:
        yield instance
    finally:
        await instance.close()


async def claimed(store, *, history=0, session=OLD_SESSION, harness="fake"):
    event = Event("chat.message", "test", {"text": "new question"}, "trigger",
                  thread_key=KEY, user_id="user")
    await store.put(event, ThreadSpec(KEY, ThreadType.CHAT, Lane.INTERACTIVE,
                                      owner_user_id="user", harness=harness))
    lease, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    if session:
        assert await store.set_session(lease, session)
    start = utcnow() - timedelta(days=1)
    messages = [Message(KEY, Role.USER if i % 2 == 0 else Role.ASSISTANT,
                        [{"type": "text", "text": f"history-{i:03d}"}],
                        created_at=start + timedelta(seconds=i))
                for i in range(history)]
    assert await store.add_messages(lease, messages)
    return lease


def executor(store, harness, tmp_path, **kwargs):
    return RunExecutor(store, {harness.name: harness}, RunEventBus(),
                       RunConfig(data_dir=tmp_path / "data", heartbeat_seconds=0.01),
                       PriceTable([]), **kwargs)


async def test_missing_session_retries_once_in_same_run_with_last_twenty_messages(store, tmp_path):
    harness = FakeHarness(HarnessError(f"No session found with id {OLD_SESSION}"))
    tokens = RunTokens()
    runner = executor(store, harness, tmp_path, tokens=tokens, mcp_url="http://unused/mcp/")
    lease = await claimed(store, history=225)
    await runner.execute(lease)

    first, fresh = harness.requests
    assert first.session_id == OLD_SESSION and first.prompt == "new question"
    assert fresh.session_id is None and first.run_id == fresh.run_id
    assert fresh.prompt.endswith("Current turn:\nnew question")
    assert "history-204" not in fresh.prompt
    for i in range(205, 225):
        assert f"history-{i:03d}" in fresh.prompt
    assert "user:" in fresh.prompt and "assistant:" in fresh.prompt
    assert fresh.mcp_servers == first.mcp_servers
    token = fresh.mcp_servers[0].headers["Authorization"].removeprefix("Bearer ")
    assert tokens.resolve(token) is None

    run, = await store.list_runs(KEY)
    assert run.id == fresh.run_id and run.status is RunStatus.COMPLETED
    assert run.usage.input_tokens == 10
    thread = await store.get_thread(KEY)
    assert thread.session_id == "fresh-session" and thread.attempts == 0
    assert await store.pending_events(KEY) == []
    messages = await store.list_messages(KEY, limit=1000)
    assert len(messages) == 227  # history + one trigger + one successful reply
    assert [m.role for m in messages[-2:]] == [Role.USER, Role.ASSISTANT]
    steps = await store.list_steps(run.id)
    assert [step.seq for step in steps] == list(range(1, len(steps) + 1))
    assert [step.kind for step in steps] == [
        StepKind.ERROR, StepKind.RETRY, StepKind.ASSISTANT_TEXT, StepKind.LLM_CALL,
    ]


@pytest.mark.parametrize("message", [
    "upstream connection refused", "session token not found", "session server not found",
    "authentication failed", "session rate limit exceeded", "could not find session token",
    "unknown session credential",
])
async def test_non_session_errors_do_not_recover(store, tmp_path, message):
    harness = FakeHarness(HarnessError(message, retryable=True))
    lease = await claimed(store)
    await executor(store, harness, tmp_path).execute(lease)
    assert len(harness.requests) == 1
    run, = await store.list_runs(KEY)
    assert run.status is RunStatus.FAILED and message in run.error
    assert not any(s.kind is StepKind.RETRY for s in await store.list_steps(run.id))
    assert (await store.get_thread(KEY)).session_id == OLD_SESSION
    assert len(await store.pending_events(KEY)) == 1


@pytest.mark.parametrize("message", [
    "session not found", f"Session '{OLD_SESSION}' does not exist",
    f"Could not find rollout for session {OLD_SESSION}",
    f"Codex exited with status 1: No session found with id {OLD_SESSION}",
])
async def test_session_error_messages_recover(store, tmp_path, message):
    harness = FakeHarness(HarnessError(message))
    await executor(store, harness, tmp_path).execute(await claimed(store))
    assert len(harness.requests) == 2
    assert harness.requests[1].session_id is None


async def test_fresh_session_failure_is_not_retried_again_and_session_is_cleared(store, tmp_path):
    harness = FakeHarness(HarnessError("session not found"), HarnessError("session not found"))
    runner = executor(store, harness, tmp_path)
    await runner.execute(await claimed(store))
    assert len(harness.requests) == 2
    thread = await store.get_thread(KEY)
    assert thread.session_id == "" and thread.attempts == 1
    run, = await store.list_runs(KEY)
    assert run.status is RunStatus.FAILED
    assert sum(s.kind is StepKind.RETRY for s in await store.list_steps(run.id)) == 1
    assert await store.list_messages(KEY) == []


async def test_new_session_error_does_not_trigger_recovery(store, tmp_path):
    harness = FakeHarness(fresh_error=HarnessError("session not found"))
    await executor(store, harness, tmp_path).execute(await claimed(store, session=None))
    assert len(harness.requests) == 1
    run, = await store.list_runs(KEY)
    assert run.status is RunStatus.FAILED
    assert not any(s.kind is StepKind.RETRY for s in await store.list_steps(run.id))


async def test_missing_codex_rollout_starts_fresh_without_attempting_resume(store, tmp_path):
    harness = FakeHarness()
    harness.name = "codex"
    await executor(store, harness, tmp_path).execute(await claimed(store, history=2, harness="codex"))
    req, = harness.requests
    assert req.session_id is None and "history-001" in req.prompt
    run, = await store.list_runs(KEY)
    assert run.status is RunStatus.COMPLETED
    retry, = [s for s in await store.list_steps(run.id) if s.kind is StepKind.RETRY]
    assert "rollout" in retry.data["reason"]


@pytest.mark.parametrize("directory", ["sessions/2026/10/17", "archived_sessions"])
async def test_restored_codex_rollout_resumes_with_original_prompt(store, tmp_path, directory):
    files = LocalFileStore(tmp_path / "remote")
    slug = thread_slug(KEY)
    await files.put(f"homes/{slug}/{directory}/rollout-2026-10-17-{OLD_SESSION}.jsonl", b"session")
    await files.put(f"workspaces/{slug}/restored.txt", b"project")
    harness = FakeHarness()
    harness.name = "codex"

    def check_restored(req):
        assert (req.workspace / "restored.txt").read_bytes() == b"project"
        assert list(req.home.rglob(f"rollout-*-{OLD_SESSION}.jsonl"))

    harness.before_run = check_restored
    sync = WorkspaceSync(files, tmp_path / "data")
    runner = executor(store, harness, tmp_path, workspace_sync=sync)
    await runner.execute(await claimed(store, harness="codex"))
    req, = harness.requests
    assert req.session_id == OLD_SESSION and req.prompt == "new question"
    await sync.flush()
    assert await files.get(f"workspaces/{slug}/note.txt") == b"saved workspace"
    assert await files.get(f"homes/{slug}/auth.json") is None


@pytest.mark.parametrize("fail", [False, True])
async def test_workspace_persisted_after_success_or_failure(store, tmp_path, fail):
    files = LocalFileStore(tmp_path / "remote")
    harness = FakeHarness(fresh_error=HarnessError("failed") if fail else None)
    sync = WorkspaceSync(files, tmp_path / "data")
    runner = executor(store, harness, tmp_path, workspace_sync=sync)
    await runner.execute(await claimed(store, session=None))
    # The run itself never uploads; the thread is queued for the next flush.
    assert await files.list() == [] and sync.dirty == {thread_slug(KEY)}
    assert await sync.flush() == 1 and not sync.dirty
    assert await files.get(f"workspaces/{thread_slug(KEY)}/note.txt") == b"saved workspace"
    assert not any("auth.json" in key for key in await files.list())
    run, = await store.list_runs(KEY)
    assert run.status is (RunStatus.FAILED if fail else RunStatus.COMPLETED)


async def test_session_clear_is_fenced_before_retry(store, tmp_path, monkeypatch):
    lease = await claimed(store)
    original = store.set_session
    cleared = []

    async def reject_clear(actual_lease, session_id):
        if session_id == "":
            cleared.append(actual_lease)
            return False
        return await original(actual_lease, session_id)

    monkeypatch.setattr(store, "set_session", reject_clear)
    harness = FakeHarness(HarnessError("session not found"))
    await executor(store, harness, tmp_path).execute(lease)
    assert cleared == [lease] and len(harness.requests) == 1
    assert (await store.get_thread(KEY)).session_id == OLD_SESSION
    run, = await store.list_runs(KEY)
    assert not any(s.kind is StepKind.RETRY for s in await store.list_steps(run.id))


async def test_run_finishes_without_waiting_for_workspace_upload(store, tmp_path):
    uploads = []

    class SlowSync(WorkspaceSync):
        async def persist(self, slug):
            uploads.append(slug)
            await asyncio.sleep(3600)

    sync = SlowSync(LocalFileStore(tmp_path / "remote"), tmp_path / "data")
    runner = executor(store, FakeHarness(), tmp_path, workspace_sync=sync)
    await asyncio.wait_for(runner.execute(await claimed(store, session=None)), 2)
    assert uploads == [] and sync.dirty == {thread_slug(KEY)}
    assert (await store.get_thread(KEY)).lease_owner is None
    run, = await store.list_runs(KEY)
    assert run.status is RunStatus.COMPLETED


async def test_failed_flush_keeps_thread_dirty(store, tmp_path):
    class BrokenSync(WorkspaceSync):
        async def persist(self, slug):
            raise OSError("backup unavailable")

    sync = BrokenSync(LocalFileStore(tmp_path / "remote"), tmp_path / "data")
    sync.mark_dirty("t1")
    assert await sync.flush() == 0 and sync.dirty == {"t1"}


async def test_session_error_after_tool_execution_does_not_replay(store, tmp_path):
    class ToolHarness(FakeHarness):
        async def run(self, req):
            self.requests.append(req)
            yield RunEvent(RunEventKind.TOOL_STARTED, {"id": "send", "tool": "send_email"})
            raise HarnessError("session not found")

    harness = ToolHarness()
    await executor(store, harness, tmp_path).execute(await claimed(store))
    assert len(harness.requests) == 1
    assert (await store.get_thread(KEY)).session_id == OLD_SESSION


async def test_persistence_failure_preserves_original_retryable_error(store, tmp_path):
    class BrokenSync(WorkspaceSync):
        async def persist(self, slug):
            raise OSError("backup unavailable")

    harness = FakeHarness(fresh_error=HarnessError("transient outage", retryable=True))
    runner = executor(store, harness, tmp_path,
                      workspace_sync=BrokenSync(None, tmp_path / "data"))
    await runner.execute(await claimed(store, session=None))
    run, = await store.list_runs(KEY)
    assert "transient outage" in run.error
    assert (await store.get_thread(KEY)).attempts == 1


async def test_lease_loss_does_not_persist_stale_workspace(store, tmp_path, monkeypatch):
    persisted = []

    class TrackingSync(WorkspaceSync):
        async def persist(self, slug):
            persisted.append(slug)

    lease = await claimed(store)

    async def lost(*args):
        return False

    monkeypatch.setattr(store, "set_session", lost)
    runner = executor(store, FakeHarness(), tmp_path,
                      workspace_sync=TrackingSync(None, tmp_path / "data"))
    await runner.execute(lease)
    assert persisted == [] and not runner.workspace_sync.dirty
