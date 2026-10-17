import pytest

from handler.runtime import server
from handler.runtime.files import LocalFileStore
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.workspaces import WorkspaceSync


def test_file_store_cli_and_environment(monkeypatch):
    monkeypatch.delenv("HANDLER_FILE_STORE", raising=False)
    assert server._parse_args(["serve"]).file_store == "local"
    assert server._parse_args(["serve", "--file-store", "s3"]).file_store == "s3"
    monkeypatch.setenv("HANDLER_FILE_STORE", "s3")
    assert server._parse_args(["serve"]).file_store == "s3"
    assert server._parse_args(["serve", "--file-store", "local"]).file_store == "local"
    with pytest.raises(SystemExit):
        server._parse_args(["serve", "--file-store", "unknown"])


@pytest.mark.parametrize("backend", ["local", "s3"])
async def test_server_wires_workspace_sync_without_starting_services(tmp_path, monkeypatch, backend):
    store = SQLiteStore(tmp_path / "store.db")
    monkeypatch.setattr(server, "SQLiteStore", lambda path: store)
    monkeypatch.setattr(server, "codex_binary", lambda: "unused")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HANDLER_FILE_PREFIX", "handler_crontal")
    captured = {}
    remote = LocalFileStore(tmp_path / "fake-s3")

    def from_env(*, prefix):
        captured["prefix"] = prefix
        return remote

    monkeypatch.setattr(server.S3FileStore, "from_env", from_env)

    class WiringComplete(Exception):
        pass

    def capture_executor(*args, **kwargs):
        captured["sync"] = kwargs["workspace_sync"]
        raise WiringComplete

    monkeypatch.setattr(server, "RunExecutor", capture_executor)
    args = server._parse_args(["serve", "--data-dir", str(tmp_path / "data"),
                               "--file-store", backend])
    try:
        with pytest.raises(WiringComplete):
            await server.serve(args)
        sync = captured["sync"]
        assert isinstance(sync, WorkspaceSync) and sync.local_root == tmp_path / "data"
        if backend == "s3":
            assert sync.file_store is remote and captured["prefix"] == "handler_crontal"
        else:
            assert isinstance(sync.file_store, LocalFileStore)
            assert "prefix" not in captured
            await sync.file_store.put("probe", b"local")
            assert (tmp_path / "data/files/probe").read_bytes() == b"local"
    finally:
        await store.close()
