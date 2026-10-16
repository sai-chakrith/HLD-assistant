"""scripts/reingest_doc.py removes one document's derived data; scripts/ingest_heldout.py never regenerates frozen v4."""
from __future__ import annotations

import pytest

from app import extract
from app.ingest import IngestRejected, ingest_document, processed_path
from scripts.ingest_heldout import ingest_heldout
from scripts.reingest_doc import remove_document


def test_remove_then_reingest(conn, dirs, make_pdf, tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf")
    ingest_document(pdf, "p1", "hld", "1.0", "synthetic", "admin", doc_id="a", conn=conn, **dirs)
    extract.extract_document("p1", "a", conn=conn, processed_dir=dirs["processed_dir"])
    ingest_document(make_pdf(tmp_path / "b.pdf", "beta"), "p1", "hld", "1.0", "synthetic", "admin", doc_id="b",
                    conn=conn, **dirs)
    with pytest.raises(IngestRejected):  # duplicate before removal
        ingest_document(pdf, "p1", "hld", "1.0", "synthetic", "admin", doc_id="a", conn=conn, **dirs)
    removed = remove_document("p1", "a", conn=conn, **dirs)
    assert removed["documents"] == 1 and removed["components"] >= 1
    assert not processed_path("p1", "a", dirs["processed_dir"]).exists()
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE doc_id='b'").fetchone()[0] == 1  # other doc untouched
    ingest_document(pdf, "p1", "hld", "1.0", "synthetic", "admin", doc_id="a", conn=conn, **dirs)


def test_v4_is_never_regenerated(conn, dirs, tmp_path):
    with pytest.raises(FileNotFoundError, match="heldout_v4.lock"):
        ingest_heldout(conn, synthetic_dir=tmp_path / "empty", doc="v4", index=False, **dirs)
