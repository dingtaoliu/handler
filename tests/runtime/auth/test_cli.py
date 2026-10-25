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
        assert await store.fail(lease, "BadRequestError: No tool output found", None)
    finally:
        await store.close()
    assert invoke(tmp_path, "thread", "revive", "chat:1:missing").returncode == 1
    revived = invoke(tmp_path, "thread", "revive", key, "--reset-session")
    assert revived.returncode == 0, revived.stderr
    assert "dead True -> False" in revived.stdout and "conv_broken -> -" in revived.stdout
    store = SQLiteStore(tmp_path / "handler.db")
    try:
        thread = await store.get_thread(key)
        assert not thread.dead and thread.attempts == 0 and not thread.session_id
        assert len(await store.pending_events(key)) == 1  # the lost message runs again
    finally:
        await store.close()
