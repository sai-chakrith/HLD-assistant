"""Unit tests for the rule functions (hand-built inventories) and findings storage."""
from __future__ import annotations

import json

from app import checks, controls
from app.extract import normalize_name


def comp(name, desc="d"):
    return {"name": name, "norm_name": normalize_name(name), "description": desc, "doc_id": "d", "version": "1",
            "page": 3, "section": "2 X", "sources": [{"doc_id": "d", "version": "1", "page": 3, "section": "2 X"}]}


def port(c, name, direction, iface, kind=""):
    r = comp(name)
    r.update(component=c, direction=direction, interface=iface, kind=kind or "unknown")
    return r


def itf(name, kind="sender_receiver"):
    r = comp(name)
    r["kind"] = kind
    return r


def sig(name, sender, receivers):
    r = comp(name)
    r.update(sender=sender, receivers=receivers)
    return r


def inv(**kw):
    base = {"components": [], "ports": [], "interfaces": [], "signals": []}
    base.update(kw)
    return base


def test_m1_required_without_provider():
    i = inv(ports=[port("A", "Rp", "required", "I1"), port("B", "Pp", "provided", "I2"),
                   port("C", "Rp2", "required", "I2")])
    f = checks.rule_m1(i)
    assert [x.entities for x in f] == [["A.Rp"]] and f[0].evidence[0].page == 3


def test_m1_provider_same_component_does_not_count():
    assert len(checks.rule_m1(inv(ports=[port("A", "Rp", "required", "I1"), port("A", "Pp", "provided", "I1")]))) == 1


def test_m2_port_kind_differs_from_interface():
    i = inv(interfaces=[itf("I1", "sender_receiver")],
            ports=[port("A", "Pp", "provided", "I1", "sender_receiver"),
                   port("B", "Rp", "required", "I1", "client_server")])
    f = checks.rule_m2(i)
    assert len(f) == 1 and f[0].entities == ["I1", "B.Rp"]


def test_m2_no_finding_when_consistent_or_unknown():
    i = inv(interfaces=[itf("I1", "client_server")],
            ports=[port("A", "Pp", "provided", "I1", "client_server"), port("B", "Rp", "required", "I1")])
    assert checks.rule_m2(i) == []


def test_m3_sender_without_receivers_and_reverse():
    f = checks.rule_m3(inv(signals=[sig("S1", "A", []), sig("S2", "", ["B"]), sig("S3", "A", ["B"]), sig("S4", "", [])]))
    assert [x.entities[0] for x in f] == ["S1", "S2"]


def test_m4_near_duplicates():
    i = inv(components=[comp("WinCtrlManager"), comp("WinCtrlManger"), comp("MotorDriver")],
            interfaces=[itf("ISwitchCmd"), itf("IMotorCmd")])
    f = checks.rule_m4(i, threshold=88)
    assert len(f) == 1 and f[0].entities == ["WinCtrlManager", "WinCtrlManger"]


def test_c1_component_absent_from_flows():
    flows = [{"section_id": "5.1", "title": "Functional Flow X", "page": 8,
              "text": "1. Alpha sends Sig to Beta. 2. Beta replies."}]
    f = checks.rule_c1(inv(components=[comp("Alpha"), comp("Beta"), comp("Gamma")]), flows)
    assert [x.entities for x in f] == [["Gamma"]]
    assert checks.rule_c1(inv(components=[comp("Gamma")]), []) == []  # no flow sections -> skipped


def test_c2_port_without_interface():
    f = checks.rule_c2(inv(ports=[port("A", "P1", "provided", ""), port("A", "P2", "provided", "I")]))
    assert [x.entities for x in f] == [["A.P1"]]


def test_c3_interface_never_defined():
    i = inv(interfaces=[itf("I1")], ports=[port("A", "P", "provided", "I1"), port("A", "Q", "provided", "Ghost"),
                                           port("B", "R", "required", "ghost")])
    f = checks.rule_c3(i)
    assert len(f) == 1 and f[0].entities == ["Ghost"]


def test_c4_missing_description():
    f = checks.rule_c4(inv(components=[comp("A", ""), comp("B", "ok")]))
    assert [x.entities for x in f] == [["A"]]


def _store_doc(conn, dirs, status="synthetic"):
    controls.add_member("p1", "admin", conn=conn)
    d = {"doc_id": "doc1", "project_id": "p1", "version": "1.0", "source_status": status,
         "sections": [{"section_id": "0", "title": "Front", "page_start": 1, "text": "", "tables": []},
                      {"section_id": "5", "title": "Functional Flow", "page_start": 4, "text": "A does x", "tables": []}]}
    p = dirs["processed_dir"] / "p1"
    p.mkdir(parents=True)
    (p / "doc1.json").write_text(json.dumps(d), encoding="utf-8")
    from app import extract
    extract.ensure_schema(conn)
    conn.execute("INSERT INTO components(project_id, doc_id, version, name, norm_name, fields, page, section, "
                 "sources_json, extraction_method) VALUES('p1','doc1','1.0','A','a','{\"description\": \"\"}',3,'2 X',"
                 "'[{\"doc_id\":\"doc1\",\"version\":\"1.0\",\"page\":3,\"section\":\"2 X\"}]','table')")
    conn.commit()


def test_run_checks_storage_idempotent_and_labels(conn, dirs):
    _store_doc(conn, dirs)
    f1 = checks.run_checks("p1", "doc1", "1.0", conn=conn, processed_dir=dirs["processed_dir"])
    f2 = checks.run_checks("p1", "doc1", "1.0", conn=conn, processed_dir=dirs["processed_dir"])
    stored = checks.get_findings("p1", "doc1", conn=conn)
    assert [f.rule_id for f in f1] == ["C4"] and len(stored) == 1 and f2[0].id == stored[0].id
    assert stored[0].review_status == "unreviewed" and stored[0].label == ""
    assert stored[0].evidence[0].page == 3 and checks.get_findings("p2", conn=conn) == []
    controls.review("finding", stored[0].id, "approved", "ok", "rev", project_id="p1", conn=conn)
    assert checks.get_findings("p1", conn=conn)[0].review_status == "approved"


def test_real_docs_get_potential_label(conn, dirs):
    _store_doc(conn, dirs, status="approved")
    f = checks.run_checks("p1", "doc1", "1.0", conn=conn, processed_dir=dirs["processed_dir"])
    assert f[0].label == "potential, needs review"
