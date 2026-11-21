"""CLI provisioning persists users and prints recoverable tokens only once."""
import os
import subprocess
import sys

from handler.runtime.store.sqlite import SQLiteStore


def invoke(data_dir, *args):
    return subprocess.run(
        [sys.executable, "-m", "handler.runtime.server", *args, "--data-dir", str(data_dir)],
        env={**os.environ, "DATABASE_URL": "", "HANDLER_DB_SCHEMA": ""},
        capture_output=True, text=True, timeout=30,
    )


async def test_cli_user_and_token_roundtrip(tmp_path):
    created = invoke(tmp_path, "user", "add", "alice", "--admin", "--display-name", "Alice")
    assert created.returncode == 0, created.stderr
    assert "created admin alice" in created.stdout
    duplicate = invoke(tmp_path, "user", "add", "alice")
    assert duplicate.returncode == 1
    issued = invoke(tmp_path, "token", "create", "alice", "--label", "laptop")
    assert issued.returncode == 0, issued.stderr
    token = issued.stdout.strip()
    assert len(token) >= 43 and token not in issued.stderr
    store = SQLiteStore(tmp_path / "handler.db")
    try:
        user = await store.resolve_token(token)
        assert (user.id, user.role, user.display_name) == ("alice", "admin", "Alice")
        assert (await store.list_tokens("alice"))[0].label == "laptop"
    finally:
        await store.close()
    assert invoke(tmp_path, "user", "add", "a:b").returncode == 2
    assert invoke(tmp_path, "token", "create", "missing").returncode == 1


async def test_cli_thread_revive(tmp_path):
    from handler.runtime.types import Event, Lane, ThreadSpec, ThreadType

    key = "chat:1:dead"
    store = SQLiteStore(tmp_path / "handler.db")
    try:
        await store.put(Event("chat.message", "test", {"text": "hi"}, "e1", thread_key=key),
                        ThreadSpec(key, ThreadType.CHAT, Lane.INTERACTIVE, harness="fake"))
        lease, = await store.claim("w", Lane.INTERACTIVE, 1, 60)
        assert await store.set_session(lease, "conv_broken")
        assert await store.fail(lease, "BadRequestError: No tool output found", 3600)
    finally:
        await store.close()
    assert invoke(tmp_path, "thread", "revive", "chat:1:missing").returncode == 1
    revived = invoke(tmp_path, "thread", "revive", key, "--reset-session")
    assert revived.returncode == 0, revived.stderr
    assert "attempts 1 -> 0" in revived.stdout and "conv_broken -> -" in revived.stdout
    store = SQLiteStore(tmp_path / "handler.db")
    try:
        thread = await store.get_thread(key)
        assert thread.available_at is None and thread.attempts == 0 and not thread.session_id
        assert len(await store.pending_events(key)) == 1  # the lost message runs again
    finally:
        await store.close()


async def test_cli_lists_and_replays_failed_runs(tmp_path):
    from handler.runtime.types import Event, Lane, ThreadSpec, ThreadType

    key = "automation:nightly"
    store = SQLiteStore(tmp_path / "handler.db")
    try:
        await store.put(Event("automation.fired", "test", {"name": "n"}, "e1", thread_key=key),
                        ThreadSpec(key, ThreadType.AUTOMATION, Lane.BACKGROUND, harness="fake"))
        lease, = await store.claim("w", Lane.BACKGROUND, 1, 60)
        (event,) = await store.pending_events(key)
        assert await store.fail(lease, "HarnessError: down", None, run_id="run_1",
                                event_ids=[event.id])
    finally:
        await store.close()
    listed = invoke(tmp_path, "run", "failed")
    assert listed.returncode == 0, listed.stderr
    (line,) = listed.stdout.splitlines()
    assert line.startswith(f"run_1\t{key}\tautomation\t") and f"events={event.id}" in line
    assert "attempts=1" in line and "HarnessError: down" in line
    assert invoke(tmp_path, "run", "replay", "run_missing").returncode == 1
    replayed = invoke(tmp_path, "run", "replay", "run_1")
    assert replayed.returncode == 0, replayed.stderr
    assert f"1 event(s) pending again on {key}" in replayed.stdout
    again = invoke(tmp_path, "run", "replay", "run_1")
    assert again.returncode == 1 and "already replayed" in again.stderr
    assert "\treplayed " in invoke(tmp_path, "run", "failed").stdout
    store = SQLiteStore(tmp_path / "handler.db")
    try:
        assert [e.id for e in await store.pending_events(key)] == [event.id]
    finally:
        await store.close()
