from __future__ import annotations

import json

from app import extract as ex
from app.extract import (Component, Interface, Port, Signal, Source, classify_table, entities_from_table,
                         extract_document, extract_entities, extract_from_prose, get_inventory, map_headers,
                         merge_entities, normalize_name)

SRC = Source(doc_id="d", version="1", page=3, section="2 X")


def tbl(header, rows=(), kind="table"):
    return {"table_id": "t", "kind": kind, "page_start": 3, "page_end": 3, "header": header, "rows": list(rows)}


def test_header_aliases():
    assert map_headers(["Software Component", "Purpose"]) == {"component": 0, "description": 1}
    m = map_headers(["Signal Name", "Source", "Destination", "Data type"])
    assert m == {"signal": 0, "sender": 1, "receivers": 2, "data_type": 3}
    assert map_headers(["Provided/Required"]) == {"direction": 0}
    assert map_headers(["Foo"]) == {}


def test_classifier():
    assert classify_table(tbl(["Component", "Description", "Category"]))[0] == "component"
    assert classify_table(tbl(["Component", "Port", "Direction", "Interface"]))[0] == "port"
    assert classify_table(tbl(["Interface", "Kind", "Data Elements"]))[0] == "interface"
    assert classify_table(tbl(["Signal", "Sender", "Receivers", "Data Type"]))[0] == "signal"
    assert classify_table(tbl(["Date", "Version", "Changed by"]))[0] is None
    assert classify_table(tbl(["Seat Name", "Reference Text"]))[0] is None


def test_component_and_port_tables():
    comps = entities_from_table(tbl(["Component", "Description", "Category"],
                                    [["WiperManager", "Controls wipers", "application"], ["", "x", "y"]]), SRC)
    assert len(comps) == 1 and comps[0].category == "application" and comps[0].review_status == "unreviewed"
    ports = entities_from_table(tbl(["Component", "Port", "Direction", "Interface"],
                                    [["A", "p1", "Provided", "IWipe"], ["B", "p2", "R-Port", "IWipe"],
                                     ["B", "p3", "weird", "IX"]]), SRC)
    assert [p.direction for p in ports] == ["provided", "required", "unknown"]
    assert ports[0].component == "A" and ports[1].interface == "IWipe"


def test_interface_and_signal_tables():
    itf = entities_from_table(tbl(["Interface", "Kind", "Data Elements"],
                                  [["IWipe", "Sender-Receiver", "speed; angle\nmode"],
                                   ["ISvc", "Client-Server", "Start, Stop"], ["IX", "?", ""]]), SRC)
    assert [i.kind for i in itf] == ["sender_receiver", "client_server", "unknown"]
    assert itf[0].data_elements == ["speed", "angle", "mode"]
    sig = entities_from_table(tbl(["Signal", "Sender", "Receivers", "Data Type"],
                                  [["VehSpeed", "ESC", "ACC, VLC;EPB", "uint16"]]), SRC)
    assert sig[0].sender == "ESC" and sig[0].receivers == ["ACC", "VLC", "EPB"] and sig[0].data_type == "uint16"


def test_naming_illustration_skipped():
    t = tbl(["", "", "", "", "Port", "", "", "Interface", "", "Data element", ""],
            [["Single occur-", "rence", "Content", "Operation + Content", "Operation"],
             ["Example", "LowBeamInd", "DisExtLight", "display"]])
    assert entities_from_table(t, SRC) == []
    t2 = tbl(["Port", "Interface", "Data element"], [["Multiple occurrence", "a", "b"]])
    assert entities_from_table(t2, SRC) == []


def test_key_value_interface():
    t = tbl([], [["Simple button interface"], ["Description", "Used to send a request."], ["Comments", "x"]], "key_value")
    out = entities_from_table(t, SRC)
    assert len(out) == 1 and isinstance(out[0], Interface)
    assert out[0].kind == "unknown" and out[0].description == "Used to send a request."
    meta = tbl([], [["Document Title", "Foo"], ["Owner", "AUTOSAR"]], "key_value")
    assert entities_from_table(meta, SRC) == []


def test_merge_keeps_all_sources():
    a = Component(name="Wiper Manager", sources=[Source(doc_id="d", page=1, section="1 A")], extraction_method="heuristic")
    b = Component(name="WiperManager", description="moves wipers", category="app",
                  sources=[Source(doc_id="d", page=5, section="2 B")], extraction_method="table")
    m = merge_entities([a, b])
    assert len(m) == 1
    assert [s.page for s in m[0].sources] == [1, 5]
    assert m[0].description == "moves wipers" and m[0].extraction_method == "table"
    assert normalize_name("Anti-Theft System") == "antitheftsystem"


def _doc():
    sec = lambda sid, title, page, text="", tables=None: {  # noqa: E731
        "section_id": sid, "title": title, "level": sid.count(".") + 1, "parent_id": None,
        "page_start": page, "page_end": page, "text": text, "tables": tables or []}
    return {"doc_id": "d1", "project_id": "p1", "version": "1.0", "sections": [
        sec("0", "Front matter", 1),
        sec("2", "Components", 3, tables=[
            tbl(["Component", "Description", "Category"], [["WiperManager", "Controls wipers", "app"]]),
            tbl(["Component", "Port", "Direction", "Interface"],
                [["WiperManager", "WipeCmd", "Provided", "IWipe"], ["Wiper", "WipeIn", "Required", "IWipe"]]),
            tbl(["Interface", "Kind", "Data Elements"], [["IWipe", "Sender-Receiver", "speed"]]),
            tbl(["Signal", "Sender", "Receivers", "Data Type"], [["Speed", "WiperManager", "Wiper", "uint8"]]),
            tbl(["Port", "Interface", "Data element"], [["Example", "LowBeamInd", "x"]]),
        ]),
        sec("2.1", "Wiper", 4, "The Washer is an actuator component that sprays."),
    ]}


def test_extract_entities_and_every_entity_has_source():
    ents = extract_entities(_doc())
    assert {len(v) for v in (ents["components"], ents["ports"], ents["interfaces"], ents["signals"])} == {2, 1}.union(
        {len(ents["components"])})
    names = {c.name for c in ents["components"]}
    assert {"WiperManager", "Washer"} <= names
    for lst in ents.values():
        for e in lst:
            assert e.sources and e.sources[0].doc_id == "d1" and e.sources[0].page
            assert e.review_status == "unreviewed"
    wm = next(c for c in ents["components"] if c.name == "WiperManager")
    assert wm.extraction_method == "table"
    assert next(c for c in ents["components"] if c.name == "Washer").extraction_method == "heuristic"


def test_extract_document_idempotent(conn, dirs):
    d = _doc()
    p = dirs["processed_dir"] / "p1"
    p.mkdir(parents=True)
    (p / "d1.json").write_text(json.dumps(d), encoding="utf-8")
    c1 = extract_document("p1", "d1", conn=conn, processed_dir=dirs["processed_dir"])
    c2 = extract_document("p1", "d1", conn=conn, processed_dir=dirs["processed_dir"])
    assert c1 == c2 and c1["ports"] == 2 and c1["interfaces"] == 1 and c1["signals"] == 1
    inv = get_inventory("p1", "d1", conn=conn)
    assert len(inv["components"]) == c1["components"]
    r = inv["ports"][0]
    assert r["review_status"] == "unreviewed" and r["sources"] and r["page"] == 3 and r["direction"] == "provided"
    assert get_inventory("p1", "d1", version="9", conn=conn)["ports"] == []
    assert get_inventory("p2", conn=conn)["components"] == []


def test_extract_from_prose_without_llm_returns_empty():
    assert extract_from_prose({"section_id": "1", "title": "t", "text": "The Foo is a thing."}) == []


def test_container_and_generic_headings_are_not_components():
    from app import extract

    def sec(sid, title, text, parent=None):
        return {"section_id": sid, "title": title, "level": sid.count(".") + 1, "parent_id": parent,
                "page_start": 3, "page_end": 3, "text": text, "tables": []}
    prose = "This component handles the thing and is described by a sentence of reasonable length."
    secs = [sec("4", "Components", ""), sec("4.1", "Group", prose, "4"), sec("4.1.1", "Alpha", prose, "4.1"),
            sec("4.1.2", "Beta", prose, "4.1"), sec("4.2", "Overview", prose, "4"), sec("4.3", "Gamma", "", "4"),
            sec("4.4", "Delta", prose, "4")]
    doc = {"doc_id": "d", "version": "1", "sections": secs}
    names = {c.name for c in extract.heuristic_entities(doc)["components"]}
    assert names == {"Alpha", "Beta", "Delta"}  # Group = container, Overview = generic, Gamma = no prose
