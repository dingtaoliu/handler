import itertools
import os
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from handler.runtime.store.postgres import PostgresCore
from handler.runtime.store.sqlite import SQLiteCore
from handler.runtime.types import Event, Lane, Run, RunStatus, ThreadSpec, ThreadType

BIN_DIRS = ["/opt/homebrew/opt/postgresql@17/bin", "/opt/homebrew/opt/postgresql@16/bin"]


def _find(name: str) -> str | None:
    for directory in BIN_DIRS:
        path = Path(directory) / name
        if path.exists():
            return str(path)
    return shutil.which(name)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Clock:
    now: datetime = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    initdb, pg_ctl = _find("initdb"), _find("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("postgres binaries not available")
    data = tmp_path_factory.mktemp("pgdata") / "data"
    sockdir = tempfile.mkdtemp(prefix="pgs")
    port = _free_port()
    subprocess.run([initdb, "-D", str(data), "-A", "trust", "-U", "postgres", "-E", "UTF8"],
                   check=True, capture_output=True)
    options = f"-c listen_addresses='' -c unix_socket_directories='{sockdir}' -p {port} -c fsync=off"
    subprocess.run([pg_ctl, "-D", str(data), "-o", options, "-l", str(data.parent / "log"),
                    "-w", "start"], check=True, capture_output=True)
    try:
        yield f"host={sockdir} port={port} user=postgres"
    finally:
        subprocess.run([pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
                       capture_output=True)
        shutil.rmtree(sockdir, ignore_errors=True)


_names = itertools.count()


@pytest.fixture
def pg_dsn(pg_server):
    name = f"t{os.getpid()}_{next(_names)}"
    with psycopg.connect(pg_server + " dbname=postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    yield f"{pg_server} dbname={name}"
    with psycopg.connect(pg_server + " dbname=postgres", autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(params=["sqlite", "postgres"])
async def store(request, tmp_path, clock):
    if request.param == "sqlite":
        instance = SQLiteCore(tmp_path / "store.sqlite", clock=clock)
    else:
        instance = PostgresCore(request.getfixturevalue("pg_dsn"), clock=clock)
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
def spec():
    return ThreadSpec(
        "chat:u:1", ThreadType.CHAT, Lane.INTERACTIVE,
        owner_user_id="u", harness="codex", model="test-model", priority=2,
    )


@pytest.fixture
def event(clock, spec):
    def make(key: str, **kwargs) -> Event:
        values = {
            "type": "chat.message", "source": "test", "payload": {"text": key},
            "idempotency_key": key, "thread_key": spec.key, "user_id": "u",
            "received_at": clock(),
        }
        values.update(kwargs)
        return Event(**values)

    return make


@pytest.fixture
async def running(store, clock, spec, event):
    queued = event("trigger")
    await store.put(queued, spec)
    lease, = await store.claim("worker", spec.lane, 1, 60)
    run = Run("run", spec.key, lease.version, [queued.id], RunStatus.RUNNING,
              spec.harness, spec.model, started_at=clock())
    assert await store.create_run(lease, run)
    return lease, run
