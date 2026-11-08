"""PDF transport preserves all pages and avoids inline image history."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fitz
import httpx
import openai
import pytest
from agents import ItemHelpers
from agents.tool_context import ToolContext

from handler.runtime.harness.openai_agents import workspace_tools


def client():
    return SimpleNamespace(base_url="https://api.openai.com/v1", api_key="test-key",
                           organization=None, project=None,
                           files=SimpleNamespace(create=AsyncMock(
                               return_value=SimpleNamespace(id="file-test"))))


def pdf(path, count=15):
    with fitz.open() as doc:
        for number in range(count):
            doc.new_page().insert_text((40, 40), f"Invoice {number + 1}")
        doc.save(path)


async def read(tools, **args):
    tool = next(t for t in tools if t.name == "read_file")
    raw = json.dumps(args)
    ctx = ToolContext(context=None, tool_name="read_file", tool_call_id="c1",
                      tool_arguments=raw)
    return await tool.on_invoke_tool(ctx, raw)


async def test_original_pdf_all_pages_and_persistent_upload_reuse(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "scan.pdf"
    pdf(path)
    api = client()
    home = tmp_path / "home"
    tools = workspace_tools(workspace, client=api, home=home)
    first, second = await asyncio.gather(
        read(tools, path="scan.pdf", as_images=True), read(tools, path="scan.pdf"))
    assert first == second
    parts = ItemHelpers._convert_tool_output(first)
    assert parts[1] == {"type": "input_file", "file_id": "file-test"}
    assert "pages 1-15 of 15" in parts[0]["text"]
    api.files.create.assert_awaited_once()
    assert api.files.create.call_args.kwargs["file"] == (
        "scan.pdf", path.read_bytes(), "application/pdf")
    assert api.files.create.call_args.kwargs["purpose"] == "user_data"
    # A later run loads references from harness home, without exposing credentials.
    await read(workspace_tools(workspace, client=api, home=home), path="scan.pdf")
    api.files.create.assert_awaited_once()
    assert "test-key" not in (home / "openai-pdf-files.json").read_text()
    pdf(path, count=2)
    await read(tools, path="scan.pdf")
    assert api.files.create.await_count == 2
    api.api_key = "different-project-key"
    await read(tools, path="scan.pdf")
    assert api.files.create.await_count == 3


async def test_selected_pages_are_pdf_and_reused(tmp_path):
    pdf(tmp_path / "quote.pdf")
    api = client()
    tools = workspace_tools(tmp_path, client=api)
    result = await read(tools, path="quote.pdf", pages="2,5-7", as_images=True)
    assert "original pages 2,5-7 of 15" in result[0].text
    data = api.files.create.call_args.kwargs["file"][1]
    with fitz.open(stream=data, filetype="pdf") as selected:
        assert [p.get_text().strip() for p in selected] == [
            "Invoice 2", "Invoice 5", "Invoice 6", "Invoice 7"]
    await read(tools, path="quote.pdf", pages="2,5-7")
    api.files.create.assert_awaited_once()


@pytest.mark.parametrize("path,pages", [("../outside.pdf", None), ("missing.pdf", None),
                                         ("quote.pdf", "100"), ("quote.pdf", "bad")])
async def test_invalid_requests_never_upload(tmp_path, path, pages):
    pdf(tmp_path / "quote.pdf")
    api = client()
    result = await read(workspace_tools(tmp_path, client=api), path=path, pages=pages)
    assert result.startswith("error:")
    api.files.create.assert_not_awaited()


async def test_corrupt_and_encrypted_pdf_never_upload(tmp_path):
    api = client()
    tools = workspace_tools(tmp_path, client=api)
    (tmp_path / "bad.pdf").write_bytes(b"not a PDF")
    assert "could not open" in await read(tools, path="bad.pdf")
    with fitz.open() as doc:
        doc.new_page()
        doc.save(tmp_path / "locked.pdf", encryption=fitz.PDF_ENCRYPT_AES_256,
                 owner_pw="owner", user_pw="reader")
    assert "password-protected" in await read(tools, path="locked.pdf")
    api.files.create.assert_not_awaited()


async def test_upload_failure_can_retry_without_image_fallback(tmp_path):
    pdf(tmp_path / "quote.pdf")
    api = client()
    api.files.create.side_effect = openai.APIConnectionError(
        request=httpx.Request("POST", "https://api.openai.com/v1/files"))
    tools = workspace_tools(tmp_path, client=api)
    assert "PDF upload failed" in await read(tools, path="quote.pdf", as_images=True)
    api.files.create.side_effect = None
    result = await read(tools, path="quote.pdf")
    assert ItemHelpers._convert_tool_output(result)[1]["type"] == "input_file"
