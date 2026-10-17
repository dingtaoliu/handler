from pathlib import Path

import pytest

from handler.runtime.files import LocalFileStore
from handler.runtime.workspaces import WorkspaceSync


def write(root: Path, name: str, content: str = "data") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


async def test_round_trip_and_existing_local_directories_win(tmp_path):
    files = LocalFileStore(tmp_path / "remote")
    root = tmp_path / "first"
    workspace = root / "workspaces" / "thread"
    home = root / "harness_homes" / "thread"
    write(workspace, "project/note.txt", "workspace")
    write(home, "sessions/2026/rollout-session.jsonl", "session")
    await WorkspaceSync(files, root).persist("thread")

    assert await files.list() == [
        "homes/thread/sessions/2026/rollout-session.jsonl",
        "workspaces/thread/project/note.txt",
    ]
    restored = tmp_path / "second"
    sync = WorkspaceSync(files, restored)
    await sync.restore("thread")
    assert (restored / "workspaces/thread/project/note.txt").read_text() == "workspace"
    assert (restored / "harness_homes/thread/sessions/2026/rollout-session.jsonl").read_text() == "session"

    write(restored / "workspaces/thread", "project/note.txt", "local edit")
    await files.put("homes/thread/new.jsonl", b"remote change")
    await sync.restore("thread")
    assert (restored / "workspaces/thread/project/note.txt").read_text() == "local edit"
    assert not (restored / "harness_homes/thread/new.jsonl").exists()


async def test_restores_workspace_and_home_independently(tmp_path):
    files = LocalFileStore(tmp_path / "remote")
    await files.put("workspaces/thread/note", b"remote")
    await files.put("homes/thread/sessions/rollout-id.jsonl", b"session")
    root = tmp_path / "local"
    write(root / "workspaces/thread", "note", "local")
    await WorkspaceSync(files, root).restore("thread")
    assert (root / "workspaces/thread/note").read_text() == "local"
    assert (root / "harness_homes/thread/sessions/rollout-id.jsonl").read_bytes() == b"session"


async def test_credentials_and_symlinks_never_reach_sync_up(tmp_path):
    class InspectingStore(LocalFileStore):
        async def sync_up(self, local_dir, prefix):
            paths = [path.relative_to(local_dir).as_posix()
                     for path in local_dir.rglob("*") if path.is_file()]
            assert paths == ["safe.txt"]
            return await super().sync_up(local_dir, prefix)

    files = InspectingStore(tmp_path / "remote")
    root = tmp_path / "data"
    credential_files = (
        "auth.json", "auth.json.bak", "AUTH.JSON", ".credentials.json",
        "google_oauth_client.json", "oauth.json", "client_secret_google.json",
        "nested/credentials.toml", "secrets.yaml", "tokens.json", "oauth_token.json",
        "mcp-auth.json", ".env", ".env.production", "prod.env", ".envrc",
        "private.key", "client.pem", ".ssh/id_rsa", ".aws/credentials",
        ".config/gcloud/access_tokens.db", ".docker/config.json", ".npmrc", ".netrc",
        ".git-credentials", ".codex/config.toml",
    )
    secret = write(tmp_path, "outside/secret", "do not upload")
    for location in ("workspaces/thread", "harness_homes/thread"):
        base = root / location
        for name in credential_files:
            write(base, name, "secret")
        write(base, "safe.txt", "durable")
        (base / "file-link").symlink_to(secret)
        (base / "directory-link").symlink_to(secret.parent, target_is_directory=True)
    write(root / "harness_homes/thread", "config.toml", 'api_key = "secret"')

    await WorkspaceSync(files, root).persist("thread")
    assert await files.list() == ["homes/thread/safe.txt", "workspaces/thread/safe.txt"]
    assert (root / "harness_homes/thread/auth.json").read_text() == "secret"
    assert secret.read_text() == "do not upload"


async def test_restore_does_not_install_credentials_from_old_backups(tmp_path):
    files = LocalFileStore(tmp_path / "remote")
    await files.put("homes/thread/auth.json", b"obsolete credential")
    await files.put("homes/thread/sessions/rollout-id.jsonl", b"session")
    root = tmp_path / "data"
    await WorkspaceSync(files, root).restore("thread")
    assert not (root / "harness_homes/thread/auth.json").exists()
    assert (root / "harness_homes/thread/sessions/rollout-id.jsonl").read_bytes() == b"session"


async def test_failed_restore_does_not_leave_partial_directory(tmp_path):
    class InterruptedStore(LocalFileStore):
        fail = True

        async def sync_down(self, prefix, local_dir):
            result = await super().sync_down(prefix, local_dir)
            if self.fail:
                raise OSError("download interrupted")
            return result

    files = InterruptedStore(tmp_path / "remote")
    await files.put("workspaces/thread/note", b"complete")
    root = tmp_path / "data"
    sync = WorkspaceSync(files, root)
    with pytest.raises(OSError, match="interrupted"):
        await sync.restore("thread")
    assert not (root / "workspaces/thread").exists()
    assert not list((root / "workspaces").iterdir())
    files.fail = False
    await sync.restore("thread")
    assert (root / "workspaces/thread/note").read_bytes() == b"complete"


async def test_no_store_is_a_no_op(tmp_path):
    root = tmp_path / "absent"
    sync = WorkspaceSync(None, root)
    await sync.restore("thread")
    await sync.persist("thread")
    assert not root.exists()


@pytest.mark.parametrize("slug", ["..", ".", "../escape", "/absolute", "a/b", "a\\b", ""])
async def test_invalid_slug_cannot_escape(tmp_path, slug):
    sync = WorkspaceSync(LocalFileStore(tmp_path / "remote"), tmp_path / "data")
    with pytest.raises(ValueError, match="slug"):
        await sync.restore(slug)
    with pytest.raises(ValueError, match="slug"):
        await sync.persist(slug)
