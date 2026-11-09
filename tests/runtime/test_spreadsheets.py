import csv
import io
import json

import pytest

from handler.runtime import spreadsheets
from handler.runtime.artifacts import ArtifactStore
from handler.runtime.context import RunContext
from handler.runtime.files import LocalFileStore
from handler.runtime.harness.base import RunEvent, RunEventKind
from handler.runtime.run import transcript_message
from handler.runtime.tools.spreadsheets import SpreadsheetTools


COLUMNS = [{"key": "id", "heading": "编号", "type": "text"},
           {"key": "price", "heading": "Price", "type": "number"}]


def test_csv_roundtrip_unicode_quoting_and_types(tmp_path):
    columns = COLUMNS + [{"key": "active", "heading": "Active", "type": "boolean"},
                         {"key": "date", "heading": "Date", "type": "date"}]
    result = spreadsheets.write_csv(tmp_path, "exports/报价.csv", columns, [
        {"id": '01178, 上海\n"Tube"', "price": 0, "active": False, "date": "2026-11-08"},
        {"id": "x", "price": 1234.125}])
    data = (tmp_path / result["path"]).read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert list(csv.reader(io.StringIO(data.decode("utf-8-sig")))) == [
        ["编号", "Price", "Active", "Date"],
        ['01178, 上海\n"Tube"', "0", "false", "2026-11-08"],
        ["x", "1234.125", "", ""]]
    assert result["rows"] == 2 and result["columns"] == 4
    assert result["size"] == len(data) and result["literalized_cells"] == 0


@pytest.mark.parametrize("value", ["=1+2", "+886-1234", "-text", "@SUM(A1)", "\t=1"])
def test_formula_looking_text_is_literalized(tmp_path, value):
    receipt = spreadsheets.write_csv(tmp_path, "out.csv", COLUMNS, [{"id": value, "price": -5}])
    rows = list(csv.reader(io.StringIO((tmp_path / "out.csv").read_text(encoding="utf-8-sig"))))
    assert rows[1] == ["'" + value, "-5"]
    assert receipt["literalized_cells"] == 1


@pytest.mark.parametrize("row", [{"id": []}, {"price": True}, {"price": float("nan")},
                                  {"unknown": 1}, {"id": "\0"}])
def test_bad_data_leaves_existing_file_unchanged(tmp_path, row):
    target = tmp_path / "out.csv"
    target.write_bytes(b"original")
    with pytest.raises(ValueError):
        spreadsheets.write_csv(tmp_path, "out.csv", COLUMNS, [row], overwrite=True)
    assert target.read_bytes() == b"original"


def test_paths_overwrite_and_output_limits(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside-csv"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    for path in ["../escape.csv", "link/escape.csv", "file.xlsx"]:
        with pytest.raises(ValueError):
            spreadsheets.write_csv(tmp_path, path, COLUMNS, [])
    spreadsheets.write_csv(tmp_path, "out.csv", COLUMNS, [])
    with pytest.raises(FileExistsError):
        spreadsheets.write_csv(tmp_path, "out.csv", COLUMNS, [])
    monkeypatch.setattr(spreadsheets, "MAX_BYTES", 30)
    with pytest.raises(ValueError, match="exceeds"):
        spreadsheets.write_csv(tmp_path, "large.csv", COLUMNS, [{"id": "a" * 50}])
    assert not (tmp_path / "large.csv").exists()


async def test_shared_tool_persists_download_and_transcript_attachment(tmp_path):
    files = LocalFileStore(tmp_path / "durable")
    artifacts = ArtifactStore(files)
    tools = SpreadsheetTools(lambda _: tmp_path / "workspace", artifacts)
    result = await tools.tools()[0].handler(RunContext("r", "chat:alice:c", "alice", "alice"),
        {"path": "table.csv", "columns": COLUMNS, "rows": [{"id": "01178", "price": 0}]})
    assert not result.is_error
    receipt = result.content
    assert receipt["status"] == "ready_to_download"
    assert "rows" in receipt and "01178" not in json.dumps(receipt)
    file_id = receipt["artifact"]["id"]
    # Reload metadata and bytes without the workspace or an UploadStore cache.
    restored = ArtifactStore(LocalFileStore(tmp_path / "durable"))
    assert (await restored.get(file_id)).user_id == "alice"
    assert b"01178,0" in await restored.data(file_id)
    for output in [receipt, json.dumps(receipt), [{"type": "text", "text": json.dumps(receipt)}],
                   {"content": [{"type": "text", "text": json.dumps(receipt)}]}]:
        event = RunEvent(RunEventKind.TOOL_FINISHED, {"id": "call", "tool": "write_spreadsheet",
            "output": output, "status": "completed"})
        message = transcript_message("chat:alice:c", "r", event)
        assert message.blocks[-1] == {"type": "file", "file_id": file_id,
                                      "filename": "table.csv", "content_type": "text/csv"}
    assert await restored.get("../escape") is None
    assert await restored.data("../escape") is None
