from __future__ import annotations

import json

import pytest

from app.extract import extract_document
from app.graph import (build_graph, dependencies_of, dependents_of, export_json, export_pyvis, graph_dir, paths)
from tests.test_extract import tbl


def _doc():
    return {"doc_id": "g1", "project_id": "p1", "version": "1.0", "sections": [
        {"section_id": "1", "title": "Arch", "level": 1, "parent_id": None, "page_start": 2, "page_end": 2,
         "text": "", "tables": [
             tbl(["Component", "Description", "Category"],
                 [["Sensor", "reads", "x"], ["Ctrl", "decides", "x"], ["Act", "acts", "x"]]),
             tbl(["Component", "Port", "Direction", "Interface"],
                 [["Sensor", "pOut", "Provided", "ISens"], ["Ctrl", "rIn", "Required", "ISens"],
                  ["Ctrl", "pCmd", "Provided", "ICmd"], ["Act", "rCmd", "Required", "ICmd"]]),
             tbl(["Interface", "Kind", "Data Elements"], [["ISens", "Sender-Receiver", "v"], ["ICmd", "Client-Server", "go"]]),
             tbl(["Signal", "Sender", "Receivers", "Data Type"], [["Alarm", "Act", "Sensor", "bool"]]),
         ]}]}


@pytest.fixture
def g(conn, dirs):
    p = dirs["processed_dir"] / "p1"
    p.mkdir(parents=True)
    (p / "g1.json").write_text(json.dumps(_doc()), encoding="utf-8")
    extract_document("p1", "g1", conn=conn, processed_dir=dirs["processed_dir"])
    return build_graph("p1", "g1", "1.0", conn)


def test_nodes_and_edges(g):
    assert g.nodes["component:Sensor"]["type"] == "component"
    assert g.nodes["component:Sensor"]["citation"]
    assert g.nodes["port:Sensor.pOut"]["direction"] == "provided"
    kinds = {k for _, _, k in g.edges(data="type")}
    assert {"has_port", "uses_interface", "provides_to", "sends", "receives"} <= kinds
    assert g.has_edge("port:Sensor.pOut", "port:Ctrl.rIn")
    assert g.has_edge("port:Ctrl.pCmd", "port:Act.rCmd")
    assert not g.has_edge("port:Sensor.pOut", "port:Act.rCmd")
    assert g.has_edge("component:Sensor", "port:Sensor.pOut")
    assert g.has_edge("port:Ctrl.rIn", "interface:ISens")


def test_dependencies_dependents_paths(g):
    assert dependencies_of(g, "Ctrl") == ["Sensor"]
    assert dependents_of(g, "Ctrl") == ["Act"]
    assert dependencies_of(g, "component:Sensor") == ["Act"]  # via signal Alarm
    assert paths(g, "Sensor", "Act") == [["Sensor", "Ctrl", "Act"]]
    assert paths(g, "act", "Ctrl") == [["Act", "Sensor", "Ctrl"]]
    with pytest.raises(KeyError):
        dependencies_of(g, "Nope")


def test_exports(g, dirs):
    d = graph_dir("p1", dirs["processed_dir"])
    assert d == dirs["processed_dir"] / "p1" / "graphs"
    j = export_json(g, d / "g1.json")
    h = export_pyvis(g, d / "g1.html")
    data = json.loads(j.read_text(encoding="utf-8"))
    assert any(n["id"] == "component:Ctrl" for n in data["nodes"]) and data["edges"]
    assert "component:Ctrl" in h.read_text(encoding="utf-8")
