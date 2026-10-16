import json

import pytest

from app.controls import PermissionDenied
from app.ingest import IngestRejected, ingest_document, load_processed, parse_pdf
from app.retrieve import search


def _ingest(path, conn, dirs, **kw):
    args = dict(project_id="p1", doc_type="hld", version="R1", source_status="approved", user="admin")
    args.update(kw)
    return ingest_document(path, conn=conn, **dirs, **args)


def _actions(conn):
    return [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]


def test_ingest_ok_stores_everything(tmp_path, conn, dirs, make_pdf):
    pdf = make_pdf(tmp_path / "doc_a.pdf")
    doc_id = _ingest(pdf, conn, dirs)
    assert doc_id == "doc_a"
    row = conn.execute("SELECT * FROM documents WHERE doc_id='doc_a'").fetchone()
    assert row["project_id"] == "p1" and row["source_status"] == "approved" and len(row["sha256"]) == 64
    assert len(json.loads(row["revisions_json"])) == 2
    doc = load_processed("p1", "doc_a", dirs["processed_dir"])
    assert [s["section_id"] for s in doc["sections"]] == ["0", "1", "2", "2.1", "2.2"]
    assert "ingest" in _actions(conn)
    assert search("p1", "wiper actuator", chroma_dir=dirs["chroma_dir"])


def test_draft_status_rejected_and_audited(tmp_path, conn, dirs, make_pdf):
    pdf = make_pdf(tmp_path / "d.pdf")
    for status in ("draft", "unapproved"):
        with pytest.raises(IngestRejected):
            _ingest(pdf, conn, dirs, source_status=status)
    assert _actions(conn).count("ingest_rejected") == 2
    assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 0


def test_non_pdf_rejected(tmp_path, conn, dirs):
    f = tmp_path / "x.txt"
    f.write_text("hello")
    with pytest.raises(IngestRejected):
        _ingest(f, conn, dirs)
    fake = tmp_path / "fake.pdf"
    fake.write_text("not really a pdf")
    with pytest.raises(IngestRejected):
        _ingest(fake, conn, dirs)
    assert _actions(conn).count("ingest_rejected") == 2


def test_duplicate_sha_rejected_same_project_only(tmp_path, conn, dirs, make_pdf):
    pdf = make_pdf(tmp_path / "a.pdf")
    _ingest(pdf, conn, dirs)
    copy = tmp_path / "b.pdf"
    copy.write_bytes(pdf.read_bytes())
    with pytest.raises(IngestRejected, match="duplicate"):
        _ingest(copy, conn, dirs)
    _ingest(copy, conn, dirs, project_id="p2")  # other project is fine
    assert "ingest_rejected" in _actions(conn)


def test_only_admin_member_may_ingest(tmp_path, conn, dirs, make_pdf):
    pdf = make_pdf(tmp_path / "a.pdf")
    for u in ("rev", "view"):
        with pytest.raises(PermissionDenied):
            _ingest(pdf, conn, dirs, user=u)
    assert _actions(conn).count("ingest_denied") == 2


def test_header_footer_stripped_and_change_history(tmp_path, make_pdf):
    doc = parse_pdf(make_pdf(tmp_path / "a.pdf"), "a")
    all_text = "\n".join(s["text"] for s in doc["sections"])
    for noise in ("Explanation of Application Interfaces", "AUTOSAR CONFIDENTIAL", "Document ID 999", " of 4"):
        assert noise not in all_text
    assert doc["revisions"][0] == {"date": "23.06.2008", "version": "1.0.1", "changed_by": "Admin",
                                   "description": "Legal text revised"}
    assert len(doc["revisions"]) == 2
    assert ". . . ." not in all_text  # TOC dropped


def test_headings_text_and_tables(tmp_path, make_pdf):
    doc = parse_pdf(make_pdf(tmp_path / "a.pdf"), "a")
    secs = {s["section_id"]: s for s in doc["sections"]}
    assert secs["2.1"]["title"] == "Wiper" and secs["2.1"]["level"] == 2 and secs["2.1"]["parent_id"] == "2"
    assert "WiperManager is an actuator component" in secs["2.1"]["text"]
    t = secs["2.1"]["tables"][0]
    assert t["kind"] == "table" and t["header"] == ["Port", "Interface", "Direction"]
    assert t["rows"] == [["WipeCmd", "IWipe", "provided"], ["RainVal", "IRain", "required"]]
    assert secs["2.2"]["page_start"] == 4
