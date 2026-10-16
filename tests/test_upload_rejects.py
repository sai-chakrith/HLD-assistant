"""Uploads refused with a reason (and audit-logged): odd file names / path traversal, oversized files,
password-protected PDFs. Accepted names keep working."""
import pymupdf
import pytest
from fastapi.testclient import TestClient

from app import api, controls
from app.config import settings


@pytest.fixture
def client(tmp_path, conn, dirs):
    conn.close()
    api.app.dependency_overrides[api.get_paths] = lambda: api.Paths(tmp_path / "test.db", dirs["chroma_dir"],
                                                                     dirs["processed_dir"])
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _post(client, name, content, status="approved"):
    return client.post("/projects/p1/upload", headers={"X-User": "admin"},
                       files={"file": (name, content, "application/pdf")},
                       data={"doc_type": "hld", "version": "1.0", "source_status": status, "use_llm": "false"})


def _audit(tmp_path):
    db = controls.get_conn(tmp_path / "test.db")
    try:
        return [r[0] for r in db.execute("SELECT details FROM audit_log WHERE action='ingest_rejected'")]
    finally:
        db.close()


@pytest.mark.parametrize("name", ["..\\x.pdf", "../x.pdf", "sub/x.pdf", "..pdf.pdf", "x;rm.pdf", "<x>.pdf", ".hidden.pdf"])
def test_odd_file_names_are_rejected(client, tmp_path, make_pdf, name):
    r = _post(client, name, make_pdf(tmp_path / "a.pdf").read_bytes())
    assert r.status_code == 400 and "invalid file name" in r.json()["detail"]
    assert any("invalid file name" in d for d in _audit(tmp_path))


def test_plain_names_with_spaces_and_brackets_are_accepted(client, tmp_path, make_pdf):
    r = _post(client, "Wiper HLD (v1.0).pdf", make_pdf(tmp_path / "a.pdf").read_bytes())
    assert r.status_code == 200, r.text


def test_oversized_upload_is_rejected(client, tmp_path, make_pdf, monkeypatch):
    from dataclasses import replace

    from app import ingest
    monkeypatch.setattr(ingest, "settings", replace(settings, max_upload_mb=0.001))  # 1 kB
    r = _post(client, "big.pdf", make_pdf(tmp_path / "a.pdf").read_bytes())
    assert r.status_code == 400 and "file too large" in r.json()["detail"] and "MAX_UPLOAD_MB" in r.json()["detail"]


def test_password_protected_pdf_is_rejected(client, tmp_path, make_pdf):
    src = make_pdf(tmp_path / "a.pdf")
    with pymupdf.open(str(src)) as d:
        d.save(str(tmp_path / "locked.pdf"), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    r = _post(client, "locked.pdf", (tmp_path / "locked.pdf").read_bytes())
    assert r.status_code == 400 and "password-protected" in r.json()["detail"]
    assert any("password-protected" in d for d in _audit(tmp_path))
