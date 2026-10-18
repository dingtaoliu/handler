from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from handler.runtime.bus import RunEventBus
from handler.runtime.dispatcher import Dispatcher
from handler.runtime.harness.base import RunEvent, RunEventKind, RunRequest
from handler.runtime.harness.codex import CodexHarness
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.run import RunConfig, RunExecutor


class Capture:
    name = "fake"

    def __init__(self):
        self.requests: list[RunRequest] = []

    async def run(self, req):
        self.requests.append(req)
        yield RunEvent(RunEventKind.ASSISTANT_TEXT, {"text": "ok"})
        yield RunEvent(RunEventKind.FINAL, {"text": "ok"})

    async def interrupt(self, run_id):
        pass


async def upload(client, world, user, name, data, ctype):
    r = await client.post("/api/uploads", headers=world.h(user), files=[("files", (name, data, ctype))])
    return r.json()[0]


async def test_chat_validates_attachments(client, world):
    f = await upload(client, world, "alice", "a.txt", b"x", "text/plain")
    bad = await client.post("/api/chat", headers=world.h("bob"),
                            json={"text": "hi", "attachments": [f["id"]]})
    assert bad.status_code == 404
    missing = await client.post("/api/chat", headers=world.h("alice"),
                                json={"text": "hi", "attachments": ["0" * 32]})
    assert missing.status_code == 404
    ok = await client.post("/api/chat", headers=world.h("alice"),
                           json={"text": "hi", "conversation_id": "c", "attachments": [f["id"]]})
    assert ok.status_code == 200
    (event,) = await world.store.pending_events("chat:alice:c")
    assert event.payload == {"text": "hi", "attachments": [f["id"]]}


async def test_attachments_reach_run(client, world):
    txt = await upload(client, world, "alice", "notes.txt", b"data", "text/plain")
    img = await upload(client, world, "alice", "pic.png", b"\x89PNG", "image/png")
    await client.post("/api/chat", headers=world.h("alice"), json={
        "text": "look", "conversation_id": "c", "attachments": [txt["id"], img["id"]]})

    harness = Capture()
    prices = world.tmp_path / "prices.yaml"
    prices.write_text("prices: []\n")
    config = RunConfig(data_dir=world.tmp_path, heartbeat_seconds=1, lease_seconds=5)
    executor = RunExecutor(world.store, {"codex": harness}, RunEventBus(), config,
                           PriceTable.from_yaml(prices))
    dispatcher = Dispatcher(world.store, executor, lease_seconds=5)
    await dispatcher.claim_once()
    while dispatcher.active_count:
        await asyncio.sleep(0.01)

    (req,) = harness.requests
    assert [p.name for p in req.images] == ["pic.png"]
    assert req.prompt.endswith("Attached files: attachments/notes.txt")
    assert (req.workspace / "attachments" / "notes.txt").read_bytes() == b"data"
    assert not (req.workspace / "attachments" / "pic.png").exists()

    msgs = await world.store.list_messages("chat:alice:c")
    blocks = msgs[0].blocks
    assert blocks[0] == {"type": "text", "text": "look"}
    assert blocks[1:] == [
        {"type": "file", "file_id": txt["id"], "filename": "notes.txt", "content_type": "text/plain"},
        {"type": "file", "file_id": img["id"], "filename": "pic.png", "content_type": "image/png"},
    ]


def test_codex_argv_includes_images(tmp_path):
    images = [tmp_path / "a.png", tmp_path / "b.jpg"]
    req = RunRequest(run_id="r", thread_key="t", workspace=tmp_path / "w", home=tmp_path / "h",
                     prompt="describe", session_id="sess", images=images)
    argv = CodexHarness("codex-test")._argv(req)
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-i"] == [str(p) for p in images]
    # a following flag always ends the -i list, so the prompt is never taken as an image
    last_i = max(i for i, a in enumerate(argv) if a == "-i")
    assert argv[last_i + 2] == "-c"
    assert argv[-3:] == ["resume", "sess", "describe"]
    assert "-i" not in CodexHarness("codex-test")._argv(
        RunRequest(run_id="r", thread_key="t", workspace=tmp_path, home=tmp_path, prompt="p"))
