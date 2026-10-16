"""Diagram geometry extraction on a tiny generated vector PDF (no LLM, no real documents)."""
from __future__ import annotations

import pymupdf
import pytest

from app import diagram
from app.extract import extract_entities, normalize_name


def _arrow(page, x0, x1, y, label, label_x=None):
    """Horizontal line x0 -> x1 with a filled triangle head at x1 and a label above it."""
    sh = page.new_shape()
    d = 1 if x1 > x0 else -1
    sh.draw_line((x0, y), (x1 - 6 * d, y))
    sh.finish(color=(0, 0, 0), width=0.6)
    sh.commit()
    sh = page.new_shape()
    sh.draw_polyline([(x1 - 6 * d, y - 3), (x1, y), (x1 - 6 * d, y + 3), (x1 - 6 * d, y - 3)])
    sh.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0.3)
    sh.commit()
    page.insert_text((label_x or min(x0, x1) + 8, y - 3), label, fontsize=6)


@pytest.fixture
def diagram_pdf(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    for rect in ((60, 100, 160, 260), (320, 100, 420, 260)):
        sh = page.new_shape()
        sh.draw_rect(pymupdf.Rect(*rect))
        sh.finish(color=(0, 0, 0), width=0.8)
        sh.commit()
    page.insert_text((88, 170), "Alpha", fontsize=11)
    page.insert_text((352, 170), "Beta", fontsize=11)
    # ports inside the borders (right edge of Alpha, left edge of Beta)
    page.insert_text((135, 124), "OutPort", fontsize=6)   # right-aligned near x=160
    page.insert_text((322, 124), "InPort", fontsize=6)     # left-aligned near x=320
    page.insert_text((135, 214), "OutBack", fontsize=6)
    page.insert_text((322, 214), "InBack", fontsize=6)
    _arrow(page, 160, 320, 121, "CmdFoo / command")        # Alpha -> Beta
    _arrow(page, 320, 160, 211, "StaFoo / status")         # Beta -> Alpha
    p = tmp_path / "d.pdf"
    doc.save(str(p))
    return p


def test_boxes_titles_ports_and_signal_assignment(diagram_pdf):
    pages = diagram.analyze_pdf(diagram_pdf)
    assert len(pages) == 1
    pd = pages[0]
    assert len(pd.boxes) == 2
    assert sorted(b.title for b in pd.boxes) == ["Alpha", "Beta"]
    byname = {p.name: p for p in pd.ports}
    assert byname["OutPort"].box.title == "Alpha" and byname["OutPort"].side == "right"
    assert byname["InPort"].box.title == "Beta" and byname["InPort"].side == "left"
    assert byname["OutPort"].direction == "provided" and byname["InPort"].direction == "required"
    sig = {s.name: s for s in pd.signals}
    assert sig["CmdFoo"].sender.title == "Alpha" and [b.title for b, _ in sig["CmdFoo"].receivers] == ["Beta"]
    assert sig["StaFoo"].sender.title == "Beta" and [b.title for b, _ in sig["StaFoo"].receivers] == ["Alpha"]
    assert diagram.page_qualifies(pd)


def test_diagram_entities_have_exact_page_method_and_confidence(diagram_pdf):
    doc = {"doc_id": "d", "version": "1", "sections": [
        {"section_id": "1", "title": "Intro", "level": 1, "page_start": 1, "page_end": 1, "text": "", "tables": []}]}
    ents = diagram.extract_diagram_entities(doc, diagram_pdf)
    assert {c.name for c in ents["components"]} == {"Alpha", "Beta"}
    sigs = {s.name: s for s in ents["signals"]}
    assert sigs["CmdFoo"].sender == "Alpha" and sigs["CmdFoo"].receivers == ["Beta"]
    for lst in ents.values():
        for e in lst:
            assert e.extraction_method == "diagram" and e.confidence in ("High", "Medium")
            assert e.sources[0].page == 1 and e.sources[0].method == "diagram"
    assert sigs["CmdFoo"].confidence == "High"
    # full pipeline: convention adds roles from the Cmd/Sta prefixes without overriding the diagram sender
    full = extract_entities(doc, pdf_path=diagram_pdf)
    cmd = next(s for s in full["signals"] if s.name == "CmdFoo")
    assert cmd.extraction_method == "diagram" and cmd.sender == "Alpha"
    assert cmd.sender_role == "master" and cmd.receiver_role == "actuator"
    assert {s.method for s in cmd.sources} >= {"diagram", "convention"}
    assert normalize_name("Alpha") in {c.norm_name for c in full["components"]}


def test_page_without_labels_or_markers_is_skipped(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    sh = page.new_shape()
    sh.draw_rect(pymupdf.Rect(60, 100, 160, 260))
    sh.finish(color=(0, 0, 0))
    sh.commit()
    page.insert_text((88, 170), "Lonely", fontsize=11)
    p = tmp_path / "e.pdf"
    doc.save(str(p))
    assert diagram.extract_diagram_entities({"doc_id": "x", "sections": []}, p) == {
        "components": [], "ports": [], "signals": []}


def test_render_debug_png(diagram_pdf, tmp_path):
    out = tmp_path / "dbg.png"
    info = diagram.render_debug(diagram_pdf, 1, out)
    assert out.exists() and out.stat().st_size > 1000
    assert info["boxes"] == 2 and info["signals"] == 2


def test_text_helpers():
    assert diagram._fix_text("FFeeaattuurree") == "Feature"
    assert diagram._fix_text("  Real   word ") == "Real word"
    assert diagram.SIGNAL_LABEL.match("StaMirrorPosMemory/ status")
    assert diagram.SIGNAL_LABEL.match("BatteryVoltage / value")
    assert not diagram.SIGNAL_LABEL.match("Some sentence / with words")
