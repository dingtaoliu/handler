"""Routing boundaries that can lose content when readers move between modules."""
from pathlib import Path

import pytest
from PIL import Image

from handler.runtime import documents
from handler.runtime.documents.router import FileType, classify_file


def test_image_content_wins_over_document_extension(tmp_path):
    path = tmp_path / "actually-a-photo.xlsx"
    Image.new("RGB", (20, 10), "red").save(path, format="PNG")
    assert classify_file(path) == FileType.IMAGE
    reading = documents.read(path)
    assert reading.images[0].mime == "image/png"
    assert "shown below" in reading.text


def test_legacy_office_error_keeps_display_name_and_precedence(tmp_path):
    path = tmp_path / "old.doc"
    Image.new("RGB", (20, 10), "red").save(path, format="PNG")
    with pytest.raises(documents.ReadError, match="downloads/old.doc is a legacy Word"):
        documents.read(path, display="downloads/old.doc")
    assert documents.to_text(path) is None


def test_unknown_extensions_read_text_and_refuse_binary(tmp_path):
    path = tmp_path / "custom.data"
    path.write_text("customer-specific export\n")
    assert documents.read(path).text == "customer-specific export\n"
    # Preview support intentionally remains narrower than interactive read support.
    assert documents.to_text(path) is None
    path.write_bytes(b"\x00\x01\x02\x03")
    with pytest.raises(documents.ReadError, match="binary file"):
        documents.read(path)


def test_missing_input_is_reported_before_format_detection(tmp_path):
    with pytest.raises(documents.ReadError, match="not a file: attachments/missing.doc"):
        documents.read(tmp_path / "missing.doc", display="attachments/missing.doc")
    assert documents.to_text(tmp_path / "missing.docx") is None


@pytest.mark.parametrize("name,body,expected", [
    ("records.JSON", '{"customer": "Brava", "total": 42}', "Brava"),
    ("records.jsonl", '{"customer": "Brava"}\n{"total": 42}\n', "Brava"),
    ("records.xml", '<quote><customer>Brava</customer></quote>', "Brava"),
    ("quote.HTML", '<h1>Brava quote</h1><p>Total: 42</p>', "Brava quote"),
])
def test_structured_documents_keep_conversion_and_previews(tmp_path, name, body, expected):
    path = tmp_path / name
    path.write_text(body)
    assert expected in documents.read(path).text
    assert expected in documents.to_text(path)


def test_zip_reads_member_boundaries_and_content(tmp_path):
    import zipfile

    path = tmp_path / "quotes.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("quote.txt", "Brava quote: 42 USD")
        archive.writestr("terms.txt", "Delivery in 14 days")
    reading = documents.read(path)
    assert "quote.txt" in reading.text and "terms.txt" in reading.text
    assert "42 USD" in reading.text and "14 days" in reading.text
    assert "42 USD" in documents.to_text(path)


def test_text_paging_and_best_effort_preview_remain_distinct(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# Quote\n" + "steel item\n" * 200)
    reading = documents.read(path, max_chars=100)
    assert "offset=" in reading.text
    preview = documents.to_text(path, max_chars=100)
    assert len(preview) <= 100 and "offset=" not in preview
