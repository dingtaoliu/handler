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
from handler.runtime.harness.pdf_files import input_file_name


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


@pytest.mark.parametrize("name", ["Bill Of Lading - SHAJ81315.PDF", "Bill Of Lading - SHAJ81315.Pdf",
                                  "Bill Of Lading - SHAJ81315.pdf"])
async def test_uppercase_pdf_extension_is_uploaded_as_lowercase_pdf(tmp_path, name):
    folder = tmp_path / "mail" / "attachments"
    folder.mkdir(parents=True)
    pdf(folder / name, count=3)
    api = client()
    tools = workspace_tools(tmp_path, client=api)
    result = await read(tools, path=f"mail/attachments/{name}")
    assert ItemHelpers._convert_tool_output(result)[1]["type"] == "input_file"
    assert api.files.create.call_args.kwargs["file"][0] == "Bill Of Lading - SHAJ81315.pdf"
    # The model is still told the file's real workspace path.
    assert f"mail/attachments/{name}" in result[0].text


async def test_selected_pages_copy_gets_a_lowercase_pdf_name(tmp_path):
    pdf(tmp_path / "Quote.PDF")
    api = client()
    await read(workspace_tools(tmp_path, client=api), path="Quote.PDF", pages="2-3")
    assert api.files.create.call_args.kwargs["file"][0] == "Quote.pdf"


async def test_cached_upload_under_an_old_name_is_uploaded_again(tmp_path):
    """References cached before names were normalised (keyed on the bytes only)
    are not reused, so a file stored as "X.PDF" is replaced by an "X.pdf" upload."""
    import hashlib

    path = tmp_path / "X.PDF"
    pdf(path, count=1)
    api = client()
    home = tmp_path / "home"
    home.mkdir()
    scope = f"{api.base_url}|{api.api_key}|{api.organization}|{api.project}"
    old_key = hashlib.sha256(scope.encode() + b"\0" + path.read_bytes()).hexdigest()
    (home / "openai-pdf-files.json").write_text(json.dumps({old_key: "file-old"}))
    result = await read(workspace_tools(tmp_path, client=api, home=home), path="X.PDF")
    assert ItemHelpers._convert_tool_output(result)[1]["file_id"] == "file-test"
    assert api.files.create.call_args.kwargs["file"][0] == "X.pdf"


@pytest.mark.parametrize("name,expected", [
    ("a.PDF", "a.pdf"), ("a.pdf", "a.pdf"), ("noext", "noext.pdf"), ("a.bin", "a.bin.pdf"),
    ("x.y.Pdf", "x.y.pdf")])
def test_pdf_upload_names(name, expected):
    assert input_file_name(name, pdf=True) == expected


@pytest.mark.parametrize("name,expected", [
    ("Notes.TXT", "Notes.txt"), ("sheet.XLSX", "sheet.xlsx"), ("data.Csv", "data.csv"),
    ("tool.exe", None), ("archive.ZIP", None), ("noext", None)])
def test_other_input_file_names_lowercase_or_refuse(name, expected):
    assert input_file_name(name) == expected


async def test_unsupported_extension_is_read_locally_never_uploaded(tmp_path):
    (tmp_path / "notes.LOG").write_text("pump order 42 confirmed")
    (tmp_path / "blob.XYZ").write_bytes(b"\x00\x01\x02")
    api = client()
    tools = workspace_tools(tmp_path, client=api)
    assert "pump order 42" in await read(tools, path="notes.LOG")
    result = await read(tools, path="blob.XYZ")
    assert isinstance(result, str)  # text or a tool error, never a failed run
    api.files.create.assert_not_awaited()
