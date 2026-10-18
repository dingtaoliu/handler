from __future__ import annotations

import asyncio
import io
import os

from PIL import Image

from handler.runtime import run as run_module
from handler.runtime.bus import RunEventBus
from handler.runtime.dispatcher import Dispatcher
from handler.runtime.harness.base import RunEvent, RunEventKind, RunRequest
from handler.runtime.harness.codex import CodexHarness
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.images import MAX_IMAGES_PER_MESSAGE
from handler.runtime.run import RunConfig, RunExecutor
from handler.runtime.types import Event, Lane, ThreadSpec, ThreadType


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


def png(size=(4, 4), color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def jpeg(size=(4, 4)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (0, 0, 255)).save(buf, "JPEG")
    return buf.getvalue()


async def run_once(world) -> RunRequest:
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
    return req


async def test_attachments_reach_run(client, world):
    txt = await upload(client, world, "alice", "notes.txt", b"data", "text/plain")
    data = png()
    img = await upload(client, world, "alice", "pic.png", data, "image/png")
    await client.post("/api/chat", headers=world.h("alice"), json={
        "text": "look", "conversation_id": "c", "attachments": [txt["id"], img["id"]]})

    req = await run_once(world)
    # the image is shown to the model from its workspace copy, and kept as a file
    assert req.images == [req.workspace / "attachments" / "pic.png"]
    assert (req.workspace / "attachments" / "pic.png").read_bytes() == data
    assert (req.workspace / "attachments" / "notes.txt").read_bytes() == b"data"
    assert req.prompt.endswith(
        "Attached files: attachments/notes.txt, "
        "attachments/pic.png (image, shown to you with this message)")

    msgs = await world.store.list_messages("chat:alice:c")
    blocks = msgs[0].blocks
    assert blocks[0] == {"type": "text", "text": "look"}
    assert blocks[1:] == [
        {"type": "file", "file_id": txt["id"], "filename": "notes.txt", "content_type": "text/plain"},
        {"type": "file", "file_id": img["id"], "filename": "pic.png", "content_type": "image/png"},
    ]


async def test_upload_records_the_sniffed_image_type(client, world):
    # a pasted screenshot may arrive with a generic type or a misleading name
    f = await upload(client, world, "alice", "image.png", jpeg(), "application/octet-stream")
    assert f["content_type"] == "image/jpeg"
    fake = await upload(client, world, "alice", "x.png", b"not an image", "image/png")
    assert fake["content_type"] == "image/png"  # claim kept, but never sent as vision


async def test_non_images_and_fake_images_are_files_only(client, world):
    svg = await upload(client, world, "alice", "d.svg", b"<svg/>", "image/svg+xml")
    fake = await upload(client, world, "alice", "x.png", b"not an image", "image/png")
    await client.post("/api/chat", headers=world.h("alice"), json={
        "text": "see", "conversation_id": "c", "attachments": [svg["id"], fake["id"]]})
    req = await run_once(world)
    assert req.images == []
    assert req.prompt.endswith("Attached files: attachments/d.svg, attachments/x.png")


async def test_chat_rejects_more_than_six_images(client, world):
    ids = [(await upload(client, world, "alice", f"{i}.png", png(), "image/png"))["id"]
           for i in range(MAX_IMAGES_PER_MESSAGE + 1)]
    r = await client.post("/api/chat", headers=world.h("alice"),
                          json={"text": "hi", "attachments": ids})
    assert r.status_code == 422
    ok = await client.post("/api/chat", headers=world.h("alice"),
                           json={"text": "hi", "attachments": ids[:MAX_IMAGES_PER_MESSAGE]})
    assert ok.status_code == 200


async def test_large_images_are_downscaled_for_the_model(client, world, monkeypatch):
    monkeypatch.setattr(run_module, "MAX_IMAGE_BYTES", 600_000)
    noise = Image.frombytes("RGB", (1200, 900), os.urandom(1200 * 900 * 3))
    buf = io.BytesIO()
    noise.save(buf, "PNG")
    big = buf.getvalue()
    f = await upload(client, world, "alice", "big.png", big, "image/png")
    await client.post("/api/chat", headers=world.h("alice"), json={
        "text": "see", "conversation_id": "c", "attachments": [f["id"]]})
    req = await run_once(world)
    attachments = req.workspace / "attachments"
    assert (attachments / "big.png").read_bytes() == big  # original kept as a file
    (shown,) = req.images
    assert shown == attachments / "big.downscaled.jpg"
    assert shown.stat().st_size <= 600_000
    with Image.open(shown) as img:
        assert max(img.size) < 1200


async def test_upload_download_names_the_owner(client, world):
    f = await upload(client, world, "alice", "pic.png", png(), "image/png")
    r = await client.get(f"/api/uploads/{f['id']}", headers=world.h("alice"))
    assert r.status_code == 200
    assert r.headers["x-handler-upload-owner"] == "alice"
    assert r.headers["content-type"] == "image/png"


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


async def test_run_shows_at_most_six_images_per_message(client, world):
    # The chat route refuses more; events from other paths are capped at run time.
    ids = [(await upload(client, world, "alice", f"{i}.png", png(), "image/png"))["id"]
           for i in range(MAX_IMAGES_PER_MESSAGE + 1)]
    key = "chat:alice:c"
    await world.store.put(
        Event(type="chat.message", source="web", payload={"text": "x", "attachments": ids},
              idempotency_key="alice:cap", user_id="alice", thread_key=key),
        ThreadSpec(key=key, type=ThreadType.CHAT, lane=Lane.INTERACTIVE,
                   owner_user_id="alice", harness="codex"))
    req = await run_once(world)
    assert len(req.images) == MAX_IMAGES_PER_MESSAGE
    assert all(p.parent == req.workspace / "attachments" for p in req.images)
    assert f"attachments/{MAX_IMAGES_PER_MESSAGE}.png (image not shown" in req.prompt
    # codex gets the same workspace copies as -i values
    argv = CodexHarness("codex-test")._argv(req)
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-i"] == [str(p) for p in req.images]
