import argparse
import stat

from handler.runtime import server


def args(path):
    return argparse.Namespace(env_file=str(path), public_url="http://localhost:8100")


def test_google_config_writes_full_private_configuration(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / "env"
    env_file.write_text("# existing config\nHANDLER_RT_MODEL=gpt-test\nHANDLER_PUBLIC_URL=old\n")
    monkeypatch.setattr("builtins.input", lambda _: "client-id")
    monkeypatch.setattr(server.getpass, "getpass", lambda _: "client-secret")

    server._configure_google(args(env_file))

    values = server.read_env_file(env_file)
    assert values["HANDLER_RT_MODEL"] == "gpt-test"
    assert values["GOOGLE_OAUTH_CLIENT_ID"] == "client-id"
    assert values["GOOGLE_OAUTH_CLIENT_SECRET"] == "client-secret"
    assert values["HANDLER_PUBLIC_URL"] == "http://localhost:8100"
    assert values["GOOGLE_OAUTH_SCOPES"].split() == list(server.FULL_GOOGLE_SCOPES)
    assert len(values["HANDLER_SECRET_KEY"]) >= 48
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    output = capsys.readouterr().out
    assert "client-secret" not in output
    assert "mode 600" in output


def test_google_config_preserves_existing_secrets(tmp_path, monkeypatch):
    env_file = tmp_path / "env"
    env_file.write_text(
        "GOOGLE_OAUTH_CLIENT_ID=old-id\n"
        "GOOGLE_OAUTH_CLIENT_SECRET=old-secret\n"
        "HANDLER_SECRET_KEY=stable-key\n"
    )
    monkeypatch.setattr("builtins.input", lambda _: "")
    monkeypatch.setattr(server.getpass, "getpass", lambda _: "")

    server._configure_google(args(env_file))

    values = server.read_env_file(env_file)
    assert values["GOOGLE_OAUTH_CLIENT_ID"] == "old-id"
    assert values["GOOGLE_OAUTH_CLIENT_SECRET"] == "old-secret"
    assert values["HANDLER_SECRET_KEY"] == "stable-key"


def test_google_config_command_defaults():
    parsed = server._parse_args(["google", "configure"])
    assert parsed.command == "google"
    assert parsed.google_command == "configure"
    assert parsed.env_file == "~/.handler-rt/env"
    assert parsed.public_url == "http://localhost:8100"
