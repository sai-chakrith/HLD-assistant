"""Shared fixtures: tmp DB / chroma / processed dirs, tiny generated PDFs, forced hashing embedder."""
from __future__ import annotations

import os

os.environ["EMBEDDER"] = "hash"  # offline, fast; must be set before the embedder singleton loads
os.environ["ALLOW_HASH_QA"] = "1"  # unit tests run qa on the hash embedder; test_qa overrides this
os.environ["LLM_PROVIDER"] = "none"  # non-llm tests never touch Ollama

import pytest  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.platypus import Table, TableStyle  # noqa: E402

from app import controls, retrieve  # noqa: E402

W, H = A4


@pytest.fixture(autouse=True)
def _hash_embedder(monkeypatch):
    # Local login credentials must not change isolated test identities.
    monkeypatch.delenv("HLD_AUTH_TOKEN_HASHES", raising=False)
    retrieve.reset_embedder()
    yield
    retrieve.reset_embedder()


@pytest.fixture
def conn(tmp_path):
    c = controls.get_conn(tmp_path / "test.db")
    for p in ("p1", "p2"):
        controls.create_project(p, conn=c)
    controls.add_user("admin", "admin", conn=c)
    controls.add_user("rev", "reviewer", conn=c)
    controls.add_user("view", "viewer", conn=c)
    for u in ("admin", "rev", "view"):
        for p in ("p1", "p2"):
            controls.add_member(p, u, conn=c)
    yield c
    c.close()


@pytest.fixture
def dirs(tmp_path):
    return {"processed_dir": tmp_path / "processed", "chroma_dir": tmp_path / "chroma"}


def _table(c, data, x, y_top):
    t = Table(data)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.7, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 9)]))
    w, h = t.wrapOn(c, W, H)
    t.drawOn(c, x, y_top - h)


def _chrome(c, page_no, total):
    c.setFont("Helvetica", 9)
    c.drawString(60, H - 40, "Explanation of Application Interfaces of the")
    c.drawString(60, 40, f"{page_no} of {total}")
    c.drawString(60, 28, "Document ID 999: AUTOSAR_Test_Doc")
    c.drawString(60, 16, "- AUTOSAR CONFIDENTIAL-")


def _body(c, y, lines, size=11):
    c.setFont("Helvetica", size)
    for ln in lines:
        c.drawString(60, y, ln)
        y -= 16
    return y


@pytest.fixture
def make_pdf():
    """Return a function building a small AUTOSAR-style PDF: cover w/ change history, TOC, 2 body pages."""
    def build(path, marker="alpha"):
        c = canvas.Canvas(str(path), pagesize=A4)
        total = 4
        _chrome(c, 1, total)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(60, H - 120, "Cover")
        _table(c, [["", "Document Change History", "", ""], ["Date", "Version", "Changed by", "Change Description"],
                   ["23.06.2008", "1.0.1", "Admin", "Legal text revised"],
                   ["04.12.2007", "1.0.0", "Admin", f"Initial {marker}"]], 60, H - 200)
        c.showPage()
        _chrome(c, 2, total)
        for i, t in enumerate(["1 Purpose", "2 Components", "2.1 Wiper"]):
            c.setFont("Helvetica", 11)
            c.drawString(60, H - 150 - 16 * i, f"{t} . . . . . . . . . . . . . . . {i + 3}")
        c.showPage()
        _chrome(c, 3, total)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(60, H - 120, "1 Purpose of this Document")
        _body(c, H - 150, [f"This document explains the {marker} interfaces of the system.",
                           "It is used for tests only."])
        c.setFont("Helvetica-Bold", 16)
        c.drawString(60, H - 230, "2 Components")
        c.setFont("Helvetica-Bold", 14)
        c.drawString(60, H - 260, "2.1 Wiper")
        _body(c, H - 285, ["The WiperManager is an actuator component that moves the wiper.",
                           "It reads the RainSensor value."])
        _table(c, [["Port", "Interface", "Direction"], ["WipeCmd", "IWipe", "provided"],
                   ["RainVal", "IRain", "required"]], 60, H - 340)
        c.showPage()
        _chrome(c, 4, total)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(60, H - 120, "2.2 Washer")
        _body(c, H - 145, ["The Washer sprays fluid onto the windscreen when requested."])
        c.showPage()
        c.save()
        return path
    return build


@pytest.fixture
def syn_env(conn, dirs, tmp_path):
    """Synthetic window-lift HLD v1 ingested + extracted into project p1 (doc_id hld_v1)."""
    import importlib.util
    from pathlib import Path

    from app import extract
    from app.ingest import ingest_document

    gen = Path(__file__).resolve().parent.parent / "data" / "synthetic" / "generate_hld.py"
    spec = importlib.util.spec_from_file_location("generate_hld_fx", gen)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "syn"
    mod.generate(out)
    doc_id = ingest_document(out / "hld_v1.pdf", "p1", "hld", "1.0", "synthetic", "admin", conn=conn, **dirs)
    extract.extract_document("p1", doc_id, conn=conn, processed_dir=dirs["processed_dir"])
    return {"doc_id": doc_id, "conn": conn, **dirs}
