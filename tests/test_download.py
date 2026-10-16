import json
import sys
from pathlib import Path

import pymupdf as fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import download_data as dd  # noqa: E402


def make_pdf(path: Path) -> Path:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "hello")
    doc.save(path)
    return path


def test_validate_pdf_ok(tmp_path):
    assert dd.validate_pdf(make_pdf(tmp_path / "a.pdf")) == 1


def test_validate_pdf_rejects_html(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"<html>not a pdf</html>")
    with pytest.raises(ValueError):
        dd.validate_pdf(p)


def test_manifest(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf")
    src = {"doc_id": "a", "title": "T", "domain": "d", "release": "R1", "project_id": "p", "source_url": "u"}
    entry = dd.build_manifest_entry(src, pdf)
    out = dd.write_manifest([entry], tmp_path)
    data = json.loads(out.read_text())
    assert data[0]["pages"] == 1 and len(data[0]["sha256"]) == 64 and data[0]["source_status"] == "approved"
