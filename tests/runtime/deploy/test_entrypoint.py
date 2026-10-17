import os
import shutil
import subprocess
from pathlib import Path

import pytest

from handler.runtime import server

ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = ROOT / "deploy" / "entrypoint.sh"


def run_entrypoint(**env: str) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ["PATH"], "HANDLER_ENTRYPOINT_DRY_RUN": "1"}
    return subprocess.run(["sh", str(ENTRYPOINT)], env={**base, **env},
                          capture_output=True, text=True, check=True)


def argv(result: subprocess.CompletedProcess) -> list[str]:
    return result.stdout.splitlines()


def test_full_command_from_heroku_environment():
    out = argv(run_entrypoint(PORT="4321", DATABASE_URL="postgres://u:p@h/db",
                              CODEX_API_KEY="k"))
    assert out == [
        "handler-rt", "serve", "--host", "0.0.0.0", "--port", "4321",
        "--data-dir", "/tmp/handler", "--database-url", "postgres://u:p@h/db",
        "--db-schema", "handler_crontal",
    ]


def test_schema_and_data_dir_overrides():
    out = argv(run_entrypoint(PORT="1", DATABASE_URL="postgres://x", CODEX_API_KEY="k",
                              HANDLER_DB_SCHEMA="handler_other",
                              HANDLER_RT_DATA_DIR="/scratch"))
    assert out[out.index("--db-schema") + 1] == "handler_other"
    assert out[out.index("--data-dir") + 1] == "/scratch"


def test_without_database_url_falls_back_to_sqlite():
    out = argv(run_entrypoint(CODEX_API_KEY="k"))
    assert "--database-url" not in out and "--db-schema" not in out
    assert out[out.index("--port") + 1] == "8100"


def test_database_url_with_spaces_and_quotes_is_one_argument():
    url = "host=/tmp/s port=1 user=a'b dbname=x"
    out = argv(run_entrypoint(DATABASE_URL=url, CODEX_API_KEY="k"))
    assert out[out.index("--database-url") + 1] == url


def test_warns_when_codex_key_missing():
    result = run_entrypoint(PORT="1")
    assert "CODEX_API_KEY" in result.stderr
    assert "CODEX_API_KEY" not in run_entrypoint(CODEX_API_KEY="k").stderr


def test_secret_never_printed_in_command():
    out = run_entrypoint(CODEX_API_KEY="sk-secret", DATABASE_URL="postgres://x").stdout
    assert "sk-secret" not in out


def test_server_args_default_from_environment(monkeypatch):
    monkeypatch.setenv("PORT", "5555")
    monkeypatch.setenv("HANDLER_DB_SCHEMA", "handler_env")
    monkeypatch.setenv("HANDLER_HARNESS_ENV_FILE", "/x/env")
    args = server._parse_args(["serve"])
    assert (args.port, args.db_schema, args.harness_env_file) == (
        5555, "handler_env", "/x/env")
    assert server._parse_args(["serve", "--port", "9", "--db-schema", "s"]).port == 9


def test_load_env_file_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / "harness.env"
    env.write_text('# c\nCODEX_API_KEY="abc"\nexport OTHER=1\nKEEP=file\n\nbad line\n')
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OTHER", raising=False)
    monkeypatch.setenv("KEEP", "process")
    loaded = server.load_env_file(env)
    assert loaded == {"CODEX_API_KEY": "abc", "OTHER": "1"}
    assert os.environ["CODEX_API_KEY"] == "abc" and os.environ["KEEP"] == "process"
    monkeypatch.delenv("CODEX_API_KEY")
    monkeypatch.delenv("OTHER")


def test_heroku_yml_and_dockerfile_agree():
    text = (ROOT / "heroku.yml").read_text()
    assert "web: Dockerfile" in text and "web: deploy/entrypoint.sh" in text
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert 'CMD ["deploy/entrypoint.sh"]' in dockerfile
    assert "sha256sum -c" in dockerfile and "codex --version" in dockerfile
    assert "USER handler" in dockerfile


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("docker") is None
                    or os.environ.get("HANDLER_DOCKER_TEST") != "1",
                    reason="set HANDLER_DOCKER_TEST=1 with docker available")
def test_docker_image_runs_codex_and_handler():
    tag = "handler-rt:test"
    subprocess.run(["docker", "build", "--platform", "linux/amd64", "-t", tag, str(ROOT)],
                   check=True, capture_output=True)
    codex = subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64",
                            "--entrypoint", "codex", tag, "--version"],
                           check=True, capture_output=True, text=True)
    assert "codex-cli" in codex.stdout
    helper = subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64",
                             "--entrypoint", "handler-rt", tag, "--help"],
                            check=True, capture_output=True, text=True)
    assert "serve" in helper.stdout


def test_global_run_limit_environment(monkeypatch):
    monkeypatch.setenv("HANDLER_MAX_CONCURRENT", "1")
    assert server._parse_args(["serve"]).max_concurrent == 1
    assert server._parse_args(["serve", "--max-concurrent", "2"]).max_concurrent == 2
