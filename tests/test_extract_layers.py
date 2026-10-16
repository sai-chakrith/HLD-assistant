"""Phase C layers: merge precedence, convention inference, LLM prose extraction (mocked), review preservation."""
from __future__ import annotations

import json

import pytest

from app import controls, extract as ex
from app.extract import (Component, Port, Signal, Source, extract_document, extract_entities, extract_from_prose,
                         get_inventory, infer_by_convention, merge_entities)


def S(page, method, **kw):
    return Source(doc_id="d", version="1", page=page, section="1 A", method=method, **kw)


# ---------------------------------------------------------------- precedence / merge

def test_merge_precedence_keeps_all_sources_and_confidence():
    items = [
        Signal(name="CmdX", sender="Master", sources=[S(1, "convention")], extraction_method="convention",
               confidence="Low"),
        Signal(name="CmdX", sender="Wrong", receivers=["Act"], sources=[S(2, "llm", evidence="q")],
               extraction_method="llm", confidence="Medium"),
        Signal(name="CmdX", sender="DiagramSender", sources=[S(3, "diagram")], extraction_method="diagram",
               confidence="Medium"),
        Signal(name="CmdX", sender="TableSender", data_type="uint8", sources=[S(4, "table")],
               extraction_method="table", confidence="High"),
    ]
    m = merge_entities(items)
    assert len(m) == 1
    e = m[0]
    assert e.extraction_method == "table" and e.sender == "TableSender" and e.confidence == "High"
    assert [s.method for s in e.sources] == ["table", "diagram", "llm", "convention"]  # best evidence first
    assert {s.page for s in e.sources} == {1, 2, 3, 4}
    assert "Act" in e.receivers  # lower-ranked sources still fill empty fields


def test_convention_alone_never_exceeds_medium_and_does_not_raise_confidence():
    a = Component(name="X", sources=[S(1, "convention")], extraction_method="convention", confidence="Medium")
    b = Component(name="X", sources=[S(2, "convention")], extraction_method="convention", confidence="High")
    assert merge_entities([a, b])[0].confidence == "Medium"
    d = Component(name="Y", sources=[S(1, "diagram")], extraction_method="diagram", confidence="Medium")
    c = Component(name="Y", sources=[S(1, "convention")], extraction_method="convention", confidence="Medium")
    assert merge_entities([d, c])[0].confidence == "Medium"  # convention agreement is not independent evidence
    h = Component(name="Y", sources=[S(1, "heuristic")], extraction_method="heuristic", confidence="Medium")
    assert merge_entities([d, h])[0].confidence == "High"    # two independent methods agree


def test_signal_with_diagram_source_drops_section_start_page_heuristic():
    h = Signal(name="StaX", sources=[S(10, "heuristic")], extraction_method="heuristic", confidence="Medium")
    d = Signal(name="StaX", sender="A", receivers=["A", "B"], sources=[S(12, "diagram")],
               extraction_method="diagram", confidence="High")
    e = merge_entities([h, d])[0]
    assert [s.page for s in e.sources] == [12]
    assert e.receivers == ["B"]  # a sender is not its own receiver


# ---------------------------------------------------------------- convention

def test_convention_roles_and_direction_never_override():
    found = {"signals": [Signal(name="ReqMove", sender="Hmi", receivers=["Mgr"], sources=[S(5, "diagram")],
                                extraction_method="diagram", confidence="High"),
                         Signal(name="Plain", sources=[S(5, "diagram")], extraction_method="diagram")],
             "ports": [Port(name="Move", component="Mgr", sources=[S(5, "diagram")], extraction_method="diagram"),
                       Port(name="Move", component="Hmi", direction="required", sources=[S(5, "diagram")],
                            extraction_method="table")]}
    out = infer_by_convention(found, "d", "1")
    assert [s.name for s in out["signals"]] == ["ReqMove"]  # no prefix -> nothing inferred
    assert out["signals"][0].sender_role == "sensor" and out["signals"][0].receiver_role == "master"
    assert out["signals"][0].confidence in ("Low", "Medium")
    assert [(p.component, p.direction) for p in out["ports"]] == [("Mgr", "required")]
    assert all(p.extraction_method == "convention" and p.confidence != "High" for p in out["ports"])
    # merged: the explicit table direction of Hmi.Move is kept (convention made no claim about it)
    doc = {"doc_id": "d", "version": "1", "sections": []}
    full = merge_entities(found["ports"] + out["ports"])
    assert {(p.component, p.direction) for p in full} == {("Mgr", "required"), ("Hmi", "required")}
    assert doc


# ---------------------------------------------------------------- LLM prose extraction (mocked provider)

TEXT = ("The WiperManager is the core component and it controls the wiper movement of the vehicle. "
        "WiperManager sends the signal CmdWiperSpeed to the FrontWiper actuator component when the driver asks for it. "
        "The Rain Sensor reports the rain amount through the port StaRainAmount to the WiperManager every cycle.")


class MockProvider:
    name = "mock"

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.prompts = []

    def generate(self, prompt, system="", schema=None, model=None, purpose="", chunk_ids=None, use_cache=True):
        self.prompts.append(prompt)
        assert schema is ex.ProseExtraction and len(prompt) + len(system) < 4096 * 2  # keeps within num_ctx
        return self.payloads.pop(0) if self.payloads else {}


def sec(text=TEXT, sid="4.1", title="Wiper"):
    return {"section_id": sid, "title": title, "page_start": 7, "page_end": 7, "text": text, "tables": []}


def test_llm_keeps_grounded_and_drops_hallucinated_entities():
    payload = {
        "components": [
            {"name": "WiperManager", "description": "core", "evidence": "The WiperManager is the core component"},
            {"name": "GhostComponent", "description": "x", "evidence": "GhostComponent controls everything"},  # invented
            {"name": "FrontWiper", "evidence": "this quote is not in the section at all"},                     # bad quote
            {"name": "system", "evidence": "controls the wiper movement"},                                      # generic
        ],
        "signals": [{"name": "CmdWiperSpeed", "sender": "WiperManager", "receivers": ["FrontWiper"],
                     "evidence": "WiperManager sends the signal CmdWiperSpeed to the FrontWiper"}],
        "ports": [{"name": "StaRainAmount", "component": "Rain Sensor", "direction": "provided",
                   "evidence": "reports the rain amount through the port STARAINAMOUNT"}],  # case-insensitive quote ok
        "interfaces": [],
    }
    prov = MockProvider([payload])
    stats = {}
    out = extract_from_prose(sec(), "d", "1", provider=prov, stats=stats)
    names = {(e.entity_type, e.name) for e in out}
    assert names == {("component", "WiperManager"), ("signal", "CmdWiperSpeed"), ("port", "StaRainAmount")}
    assert stats["kept"] == 3 and stats["dropped"] == 3
    for e in out:
        assert e.extraction_method == "llm" and e.confidence == "Medium" and e.review_status == "unreviewed"
        s = e.sources[0]
        assert s.method == "llm" and s.page == 7 and s.section == "4.1 Wiper" and 0 < len(s.evidence.split()) <= 25
        assert ex._norm_text(s.evidence) in ex._norm_text(TEXT)
    assert next(e for e in out if e.name == "CmdWiperSpeed").receivers == ["FrontWiper"]


def test_llm_skips_front_matter_toc_and_short_sections_and_chunks_long_ones():
    prov = MockProvider([])
    assert extract_from_prose(sec(sid="0", title="Front matter"), provider=prov) == []
    assert extract_from_prose(sec(title="Table of Contents"), provider=prov) == []
    assert extract_from_prose(sec(text="Too short."), provider=prov) == []
    assert prov.prompts == []
    long = " ".join(f"Sentence number {i} says that WiperManager does something useful here." for i in range(200))
    extract_from_prose(sec(text=long), provider=prov)
    assert len(prov.prompts) >= 3 and all(len(p) < 3600 for p in prov.prompts)


def test_llm_removes_figure_label_noise_and_page_headers():
    noisy = "R3.2 Rev 1\nStaLightFault / status\nMirrorMove\n" + TEXT
    pt = ex.prose_text(sec(noisy))
    assert "StaLightFault" not in pt and "Rev 1" not in pt and "WiperManager" in pt


def test_use_llm_wired_into_extract_entities_and_merges_with_other_layers():
    doc = {"doc_id": "d", "version": "1", "sections": [
        {"section_id": "0", "title": "Front matter", "level": 1, "page_start": 1, "page_end": 1, "text": "", "tables": []},
        {"section_id": "2", "title": "Components", "level": 1, "page_start": 3, "page_end": 3, "text": TEXT,
         "tables": [{"table_id": "t", "kind": "table", "page_start": 3, "page_end": 3,
                     "header": ["Component", "Description", "Category"],
                     "rows": [["WiperManager", "Controls wipers", "app"]]}]}]}
    prov = MockProvider([{"components": [{"name": "WiperManager", "evidence": "The WiperManager is the core"}]}])
    without = extract_entities(doc, use_llm=False, use_convention=False)
    with_llm = extract_entities(doc, use_llm=True, provider=prov, use_convention=False)
    assert len(prov.prompts) == 1
    wm = next(c for c in with_llm["components"] if c.name == "WiperManager")
    assert wm.extraction_method == "table" and wm.description == "Controls wipers"      # table wins
    assert {s.method for s in wm.sources} == {"table", "llm"} and wm.confidence == "High"
    assert len(without["components"]) == len(with_llm["components"]) == 1


def test_llm_not_configured_returns_empty():
    assert extract_from_prose(sec(), "d", "1") == []  # LLM_PROVIDER=none in tests


@pytest.mark.llm
def test_real_llm_extracts_grounded_entities(tmp_path):
    from app import llm
    # A 3B model is unreliable on a 3-sentence toy text (it often returns nothing), so use a real passage:
    # BodyComfort R3.2.3 section 4.1 (component names embedded in sentences like "FrontWasher is an actuator component").
    # Contract asserted: non-empty on this realistic prose AND every returned entity is grounded (its name and its
    # evidence quote occur verbatim in the section text) - the anti-hallucination guarantee, not just "some output".
    from app.ingest import load_processed
    try:
        real = next(s for s in load_processed("body-comfort", "bodycomfort_r3.2.3")["sections"]
                    if s["section_id"] == "4.1")
    except (FileNotFoundError, StopIteration):
        pytest.skip("real BodyComfort document not ingested")
    prov = llm.OllamaProvider(db_path=tmp_path / "c.db", trace_path=tmp_path / "t.jsonl")
    out = extract_from_prose(real, "bodycomfort_r3.2.3", "R3.2.3", provider=prov)
    assert out, "expected grounded entities from a real component-description section"
    norm = ex._norm_text(ex.prose_text(real))
    for e in out:
        assert ex._norm_text(e.sources[0].evidence) in norm
        assert ex._norm_text(e.name) in norm
        assert e.sources[0].method == "llm" and len(e.sources[0].evidence.split()) <= 25
    assert {"FrontWiper", "FrontWasher", "NozzleHeater"} & {e.name for e in out}


# ---------------------------------------------------------------- review preservation across re-extraction

def _write(dirs, sections):
    p = dirs["processed_dir"] / "p1"
    p.mkdir(parents=True, exist_ok=True)
    (p / "d1.json").write_text(json.dumps({"doc_id": "d1", "project_id": "p1", "version": "1.0",
                                           "sections": sections}), encoding="utf-8")


def _tbl(rows):
    return {"table_id": "t", "kind": "table", "page_start": 3, "page_end": 3,
            "header": ["Component", "Port", "Direction", "Interface"], "rows": rows}


def _sections(rows, comp_rows=()):
    tables = [_tbl(rows)]
    if comp_rows:
        tables.append({"table_id": "c", "kind": "table", "page_start": 3, "page_end": 3,
                       "header": ["Component", "Description", "Category"], "rows": list(comp_rows)})
    return [{"section_id": "2", "title": "Ports", "level": 1, "page_start": 3, "page_end": 3, "text": "",
             "tables": tables}]


def test_reextraction_preserves_review_decisions_and_history(conn, dirs):
    _write(dirs, _sections([["CompA", "Port1x", "Provided", "IX"], ["CompB", "Port2x", "Required", "IX"], ["CompC", "Port3x", "Provided", "IY"]],
                           [["CompA", "first description", "app"]]))
    extract_document("p1", "d1", conn=conn, processed_dir=dirs["processed_dir"], use_diagram=False)
    inv = get_inventory("p1", "d1", conn=conn)
    pa = next(p for p in inv["ports"] if p["component"] == "CompA")
    pb = next(p for p in inv["ports"] if p["component"] == "CompB")
    ca = inv["components"][0]
    controls.review("port", pa["id"], "approved", "looks right", "rev", project_id="p1", conn=conn)
    controls.review("port", pa["id"], "corrected", "interface is IX2", "rev", project_id="p1", conn=conn)
    controls.review("component", ca["id"], "rejected", "not a component", "rev", project_id="p1", conn=conn)
    controls.review("port", pb["id"], "approved", "ok", "rev", project_id="p1", conn=conn)
    # new release: A.p1 interface changed, new port D, B.p2 vanished (reviewed -> kept), C.p3 vanished (unreviewed -> gone)
    _write(dirs, _sections([["CompA", "Port1x", "Provided", "IZ"], ["CompD", "Port4x", "Required", "IX"]],
                           [["CompA", "second description", "app"]]))
    counts = extract_document("p1", "d1", conn=conn, processed_dir=dirs["processed_dir"], use_diagram=False)
    inv2 = get_inventory("p1", "d1", conn=conn)
    pa2 = next(p for p in inv2["ports"] if p["component"] == "CompA")
    # A comment-only correction has no field override: changed machine facts need a fresh review.
    assert pa2["id"] == pa["id"] and pa2["review_status"] == "unreviewed"
    assert pa2["interface"] == "IZ"                                                # content refreshed
    assert [r["decision"] for r in controls.list_reviews("port", pa["id"], conn=conn)] == ["approved", "corrected"]
    assert [r["comment"] for r in controls.list_reviews("port", pa["id"], conn=conn)][-1] == "interface is IX2"
    ca2 = inv2["components"][0]
    assert ca2["id"] == ca["id"] and ca2["review_status"] == "rejected" and ca2["description"] == "second description"
    assert next(p for p in inv2["ports"] if p["component"] == "CompD")["review_status"] == "unreviewed"
    assert any(p["component"] == "CompB" and p["review_status"] == "approved" for p in inv2["ports"])  # reviewed: kept
    assert not any(p["component"] == "CompC" for p in inv2["ports"])                                    # unreviewed: removed
    assert counts["ports"] == 3
    # idempotent third run changes nothing
    again = extract_document("p1", "d1", conn=conn, processed_dir=dirs["processed_dir"], use_diagram=False)
    assert again == counts
    assert [p["id"] for p in get_inventory("p1", "d1", conn=conn)["ports"]] == [p["id"] for p in inv2["ports"]]
