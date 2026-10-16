"""OCR for scanned pages: detection, end-to-end (needs Tesseract), graceful fallback, chunk metadata, QA notes."""
from pathlib import Path

import pymupdf
import pytest

from app import ocr, qa
from app.chunk import chunk_document
from app.ingest import parse_pdf

SYN = Path(__file__).resolve().parent.parent / "data" / "synthetic"
SCAN = SYN / "scanned_sample.pdf"
needs_tesseract = pytest.mark.skipif(not ocr.tesseract_available(), reason="Tesseract binary not installed")


def test_scanned_detection():
    with pymupdf.open(str(SCAN)) as d:
        assert all(ocr.is_scanned(p) and not p.get_text().strip() for p in d)
    with pymupdf.open(str(SYN / "hld_v1.pdf")) as d:
        assert not any(ocr.is_scanned(p) for p in d)


@needs_tesseract
def test_ocr_end_to_end():
    doc = parse_pdf(SCAN, "scan")
    assert [p["page"] for p in doc["ocr_pages"]] == [1, 2]
    assert all(p["status"] == "ok" and p["chars"] > 100 for p in doc["ocr_pages"])
    assert doc["ocr_pages"][1]["confidence"] > 80  # prose page; the gridded table page is much noisier
    text = " ".join(s["text"] for s in doc["sections"])
    assert "MotorDriver" in text
    assert any(s.get("ocr") and s["ocr_confidence"] > 80 for s in doc["sections"])
    assert any(s["section_id"] == "5.1" for s in doc["sections"])  # numbered heading survives OCR


def test_tesseract_missing_fallback(monkeypatch):
    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)
    doc = parse_pdf(SCAN, "scan")
    assert doc["n_pages"] == 2
    assert [(p["page"], p["status"], p["chars"]) for p in doc["ocr_pages"]] == [(1, "tesseract_missing", 0),
                                                                                (2, "tesseract_missing", 0)]
    assert not any(s["text"].strip() for s in doc["sections"]) and not any(s.get("ocr") for s in doc["sections"])


def test_ocr_disabled(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(ocr, "settings", replace(ocr.settings, ocr_enabled=False))
    with pymupdf.open(str(SCAN)) as d:
        assert ocr.ocr_page(d[0])["status"] == "disabled"


def _doc(ocr_flag):
    sec = {"section_id": "1", "title": "Intro", "level": 1, "parent_id": "0", "page_start": 1, "page_end": 1,
           "text": "The WinCtrlManager validates requests.", "tables": []}
    if ocr_flag:
        sec.update(ocr=True, ocr_confidence=87.4)
    return {"doc_id": "d", "project_id": "p", "doc_type": "hld", "version": "1", "sections": [sec]}


def test_chunk_metadata_ocr_flags():
    m = chunk_document(_doc(True))[0]["metadata"]
    assert m["ocr"] is True and m["ocr_confidence"] == 87.4
    m = chunk_document(_doc(False))[0]["metadata"]
    assert m["ocr"] is False and m["ocr_confidence"] == -1.0


class FakeProvider:
    name = "fake"

    def generate(self, prompt, system="", schema=None):
        return "The manager validates requests [C1]."


def test_qa_citation_ocr_note(conn, tmp_path, monkeypatch):
    from app import retrieve
    chunks = chunk_document({**_doc(True), "project_id": "p1"})
    retrieve.index_chunks("p1", chunks, chroma_dir=tmp_path / "chroma")
    a = qa.ask("p1", "What does WinCtrlManager validate?", "view", conn=conn, chroma_dir=tmp_path / "chroma",
               provider=FakeProvider(), threshold=-1.0)
    c = a.citations[0]
    assert c.ocr and c.ocr_confidence == 87.4 and c.note == "from OCR, confidence 87"
    assert "from OCR, confidence 87" in a.limitation
    chunks = chunk_document({**_doc(False), "doc_id": "e", "project_id": "p2"})
    retrieve.index_chunks("p2", chunks, chroma_dir=tmp_path / "chroma")
    b = qa.ask("p2", "What does WinCtrlManager validate?", "view", conn=conn, chroma_dir=tmp_path / "chroma",
               provider=FakeProvider(), threshold=-1.0)
    assert not b.citations[0].ocr and b.citations[0].note == "" and "OCR" not in b.limitation
