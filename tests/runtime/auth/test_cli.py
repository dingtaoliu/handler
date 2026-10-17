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
