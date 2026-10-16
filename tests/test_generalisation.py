"""Parser generalisation: fuzzy header aliases, column-role inference, prose port rules, LLM ports with component/direction."""
from __future__ import annotations

import pytest

from app import extract as ex, headers
from app.extract import extract_entities, extract_from_prose, map_headers, classify_table
from app.ingest import _header_valid, _roles_ok


# ---------------------------------------------------------------- fuzzy header aliases

@pytest.mark.parametrize("cell,canon", [
    ("Owner SWC", "component"), ("SW Component", "component"), ("Component Prototype", "component"),
    ("Port Name", "port"), ("Port Prototype", "port"), ("P/R", "direction"), ("Role", "direction"),
    ("Provided/Required", "direction"), ("Port Direction", "direction"), ("Port Interface", "interface"),
    ("Interface Ref", "interface"), ("Pattern", "kind"), ("Communication Pattern", "kind"),
    ("Elements", "data_elements"), ("Data Element", "data_elements"), ("Producer", "sender"),
    ("Sender SWC", "sender"), ("Consumer(s)", "receivers"), ("Receivers", "receivers"), ("Recievers", "receivers"),
    ("Destination", "receivers"), ("Purpose", "description"), ("Datatype", "data_type"),
])
def test_fuzzy_header_words(cell, canon):
    assert headers.match_header(cell) == canon


@pytest.mark.parametrize("cell", ["SeatHeatCtrl", "R", "ISeatTemp", "uint8", "Regulates the seat heating level from "
                                  "the measured seat temperature", "", "42"])
def test_data_cells_are_not_headers(cell):
    assert headers.match_header(cell) is None


def test_ingest_accepts_plausible_header_rows_and_rejects_data_rows():
    assert _header_valid(["Owner SWC", "Port Name", "P/R", "Port Interface"])
    assert _header_valid(["Interface Name", "Pattern", "Elements"])
    assert _header_valid(["Data Element", "Producer", "Consumer(s)", "Type"])
    assert not _header_valid(["SeatHeatCtrl", "RpSeatTemp", "R", "ISeatTemp"])  # continuation-page data row
    assert not _header_valid(["Owner SWC"])


def test_classify_tables_with_new_wording():
    t = {"kind": "table", "header": ["Owner SWC", "Port Name", "P/R", "Port Interface"],
         "rows": [["A", "PpX", "P", "IX"]]}
    assert classify_table(t)[0] == "port"
    t = {"kind": "table", "header": ["Interface Name", "Pattern", "Elements"], "rows": [["IX", "S/R", "X"]]}
    assert classify_table(t)[0] == "interface"
    t = {"kind": "table", "header": ["Data Element", "Producer", "Consumer(s)", "Type"],
         "rows": [["X", "A", "B, C", "uint8"]]}
    kind, cm = classify_table(t)
    assert kind == "signal" and cm["signal"] == 0 and cm["sender"] == 1 and cm["receivers"] == 2
    assert ex.norm_kind("S/R") == "sender_receiver" and ex.norm_kind("C/S") == "client_server"


# ---------------------------------------------------------------- column-role inference (unknown headers)

PORT_ROWS = [["SeatHeatCtrl", "RpSeatTemp", "R", "ISeatTemp"], ["SeatHeatCtrl", "PpHeaterCmd", "P", "IHeaterCmd"],
             ["HeaterDriver", "RpHeaterCmd", "R", "IHeaterCmd"]]


def test_infer_roles_from_values():
    r = headers.infer_roles(PORT_ROWS)
    assert r == {"direction": 2, "port": 1, "interface": 3, "component": 0}
    r = headers.infer_roles([["IX", "S/R", "A"], ["IY", "C/S", "B, C"]])
    assert r["interface"] == 0 and r["kind"] == 1


def test_infer_roles_is_conservative():
    assert headers.infer_roles([["alpha", "beta"], ["gamma", "delta"]]) == {}
    assert headers.infer_roles([["SeatHeatCtrl", "SeatTempSensor"], ["HeaterDriver", "OccupancyDetector"]]) == {}
    assert headers.infer_roles([["R", "x"]]) == {}  # one row is not enough evidence
    assert headers.infer_roles([["P", "x"], ["Q", "y"]]) == {}  # mixed column: below 80%


def test_unknown_header_table_classified_by_values_and_accepted_by_ingest():
    cr = [["Foo", "Bar", "Baz", "Qux"]] + PORT_ROWS
    assert _roles_ok(cr)
    assert not _roles_ok([["Foo", "Bar", "Baz"], ["alpha", "beta", "gamma"], ["delta", "eps", "zeta"]])
    t = {"kind": "table", "header": cr[0], "rows": PORT_ROWS}
    kind, cm = classify_table(t)
    assert kind == "port" and cm == {"component": 0, "port": 1, "direction": 2, "interface": 3}
    ports = ex.entities_from_table(t, ex.Source(doc_id="d"))
    assert {(p.component, p.name, p.direction, p.interface) for p in ports} == {
        ("SeatHeatCtrl", "RpSeatTemp", "required", "ISeatTemp"), ("SeatHeatCtrl", "PpHeaterCmd", "provided", "IHeaterCmd"),
        ("HeaterDriver", "RpHeaterCmd", "required", "IHeaterCmd")}


def test_header_mapping_wins_over_inference():
    t = {"kind": "table", "header": ["Component", "Port", "Direction", "Interface", "Note"],
         "rows": PORT_ROWS}
    assert classify_table(t)[1] == {"component": 0, "port": 1, "direction": 2, "interface": 3}


# ---------------------------------------------------------------- prose port rules

def _doc(text):
    return {"doc_id": "d", "version": "1", "sections": [
        {"section_id": "3.2", "title": "Ports", "page_start": 6, "page_end": 6, "text": text, "tables": []}]}


def _ports(text):
    return {(p.component, p.name, p.direction, p.interface): p for p in extract_entities(_doc(text), use_convention=False)["ports"]}


def test_prose_ports_colon_form():
    got = _ports("SeatMemoryMgr: it offers the port PpMemPos via the interface IMemPos; and it consumes data through "
                 "the port RpHeatLevel via the interface IHeatLevel.")
    assert set(got) == {("SeatMemoryMgr", "PpMemPos", "provided", "IMemPos"),
                        ("SeatMemoryMgr", "RpHeatLevel", "required", "IHeatLevel")}
    p = got[("SeatMemoryMgr", "PpMemPos", "provided", "IMemPos")]
    assert p.extraction_method == "heuristic" and p.confidence == "Medium"
    assert p.sources[0].method == "heuristic" and p.sources[0].page == 6 and "PpMemPos" in p.sources[0].evidence


def test_prose_ports_verb_form_and_port_of_form():
    got = _ports("WiperCtrl provides the port PpWipe via the interface IWipe. "
                 "The required port RpRain of WiperCtrl is typed by IRain.")
    assert ("WiperCtrl", "PpWipe", "provided", "IWipe") in got and ("WiperCtrl", "RpRain", "required", "IRain") in got
    got = _ports("WiperCtrl requires RpRain via IRain.")
    assert ("WiperCtrl", "RpRain", "required", "IRain") in got


def test_prose_ports_ignore_non_port_text():
    assert _ports("The system provides the port interface which is described below.") == {}
    assert _ports("The controller reads the port list from the configuration file.") == {}
    assert _ports("It offers a port for diagnostics.") == {}


def test_table_port_outranks_prose_port():
    doc = _doc("SeatCtrl: it offers the port PpX via the interface IWrong.")
    doc["sections"][0]["tables"] = [{"kind": "table", "page_start": 6, "header": ["Component", "Port", "Direction", "Interface"],
                                     "rows": [["SeatCtrl", "PpX", "provided", "IRight"]]}]
    (p,) = extract_entities(doc, use_convention=False)["ports"]
    assert p.interface == "IRight" and p.extraction_method == "table"
    assert {s.method for s in p.sources} == {"table", "heuristic"}


# ---------------------------------------------------------------- LLM schema: ports with component + direction

class _Prov:
    name = "mock"

    def __init__(self, payload):
        self.payload, self.prompts, self.systems = payload, [], []

    def generate(self, prompt, system="", schema=None, model=None, purpose="", chunk_ids=None, use_cache=True):
        self.prompts.append(prompt)
        self.systems.append(system)
        assert schema is ex.ProseExtraction
        return self.payload


TEXT = ("The BlinkerCtrl component drives the lamps and talks to the lamp driver. BlinkerCtrl offers the connector "
        "PpLampCmd typed by ILampCmd to LampDriver, and it needs RpSwitchState from SwitchReader. The lamps flash "
        "with a period of one second whenever the hazard switch or the turn indicator lever is active.")


def test_llm_ports_and_signals_carry_component_direction_sender_receivers():
    payload = {"ports": [
        {"name": "PpLampCmd", "component": "BlinkerCtrl", "direction": "provided", "interface": "ILampCmd",
         "evidence": "BlinkerCtrl offers the connector PpLampCmd typed by ILampCmd"},
        {"name": "RpSwitchState", "component": "GhostComp", "direction": "required", "interface": "ISwitch",
         "evidence": "it needs RpSwitchState from SwitchReader"}],
        "signals": [{"name": "LampCmd", "sender": "BlinkerCtrl", "receivers": ["LampDriver", "Nobody"],
                     "evidence": "BlinkerCtrl sends LampCmd to LampDriver"}]}
    prov = _Prov(payload)
    sec = {"section_id": "5", "title": "Lamps", "page_start": 3, "page_end": 3,
           "text": TEXT + " BlinkerCtrl sends LampCmd to LampDriver.", "tables": []}
    out = extract_from_prose(sec, "d", "1", provider=prov)
    ports = {e.name: e for e in out if e.entity_type == "port"}
    assert ports["PpLampCmd"].component == "BlinkerCtrl" and ports["PpLampCmd"].direction == "provided"
    assert ports["PpLampCmd"].interface == "ILampCmd"
    # component / interface not present in the section text are not trusted, the port itself is still grounded
    assert ports["RpSwitchState"].component == "" and ports["RpSwitchState"].interface == "" \
        and ports["RpSwitchState"].direction == "required"
    sig = next(e for e in out if e.entity_type == "signal")
    assert sig.sender == "BlinkerCtrl" and sig.receivers == ["LampDriver"]
    assert "direction" in prov.systems[0] and "receivers" in prov.systems[0] and len(prov.systems[0]) < 1400


def _mini_pdf(path):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle
    h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16, leading=20, spaceAfter=8)
    body = ParagraphStyle("b", fontName="Helvetica", fontSize=10, leading=14, spaceAfter=8)
    t = Table([["SW Component", "Purpose", "Category"], ["Alpha", "Does alpha things", "Sensor"], ["Beta", "", "Actuator"]])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.black)]))
    SimpleDocTemplate(str(path), pagesize=A4, topMargin=120, bottomMargin=120).build(
        [Paragraph("1 Overview", h1), Paragraph("Intro text of the document.", body),
         Paragraph("2 Building Blocks", h1), t])
    return path


def test_ingest_without_toc_finds_headings_on_early_pages_and_keeps_empty_cells(tmp_path):
    from app.ingest import parse_pdf
    doc = parse_pdf(_mini_pdf(tmp_path / "m.pdf"), "m")
    assert [s["section_id"] for s in doc["sections"]] == ["0", "1", "2"]
    (tab,) = doc["sections"][2]["tables"]
    assert tab["rows"][1] == ["Beta", "", "Actuator"]  # the empty Purpose cell is kept, Category stays in place
    doc["version"] = "1"
    comps = {c.name: c for c in extract_entities(doc, use_convention=False)["components"]}
    assert comps["Beta"].description == "" and comps["Beta"].category == "Actuator"


def test_llm_port_with_ungrounded_evidence_is_dropped():
    payload = {"ports": [{"name": "PpLampCmd", "component": "BlinkerCtrl", "direction": "provided",
                          "evidence": "BlinkerCtrl exposes a totally different sentence"}]}
    sec = {"section_id": "5", "title": "Lamps", "page_start": 3, "page_end": 3, "text": TEXT, "tables": []}
    assert extract_from_prose(sec, "d", "1", provider=_Prov(payload)) == []
