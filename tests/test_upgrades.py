"""Regression coverage for evidence, corrections, review preservation, and real authentication."""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app import api, checks, controls, diagram, extract, graph, qa
from tests.test_api import client, H, _upload
from tests.test_extract_layers import _write, _sections, MockProvider, sec, TEXT
from tests.test_diagram import diagram_pdf
from tests.test_compare import two, _ent


def test_correction_survives_reextract_and_owner_change(conn, dirs):
    _write(dirs, _sections([["CompA", "PortAbc", "Provided", "IX"]]))
    extract.extract_document("p1", "d1", conn=conn, processed_dir=dirs["processed_dir"], use_diagram=False)
    row = extract.get_inventory("p1", "d1", conn=conn)["ports"][0]
    extract.correct_entity("p1", "port", row["id"], {"component": "CompB", "interface": "IReviewed"},
                           "Verified on the source page", "rev", conn)
    extract.extract_document("p1", "d1", conn=conn, processed_dir=dirs["processed_dir"], use_diagram=False)
    after = extract.get_inventory("p1", "d1", conn=conn)["ports"]
    assert len(after) == 1 and after[0]["id"] == row["id"]
    assert after[0]["component"] == "CompB" and after[0]["interface"] == "IReviewed"
    assert after[0]["review_status"] == "corrected"
    assert "interface" in after[0]["corrected_fields"]
    g = graph.build_graph("p1", "d1", "1.0", conn)
    assert "component:CompB" in g and "component:CompA" not in g


def test_changed_approved_fields_need_review_again(conn, dirs):
    _write(dirs, _sections([["CompA", "PortAbc", "Provided", "IX"]]))
    extract.extract_document("p1", "d1", conn=conn, processed_dir=dirs["processed_dir"], use_diagram=False)
    row = extract.get_inventory("p1", "d1", conn=conn)["ports"][0]
    controls.review("port", row["id"], "approved", "checked", "rev", "p1", conn)
    _write(dirs, _sections([["CompA", "PortAbc", "Required", "IX"]]))
    extract.extract_document("p1", "d1", conn=conn, processed_dir=dirs["processed_dir"], use_diagram=False)
    assert extract.get_inventory("p1", "d1", conn=conn)["ports"][0]["review_status"] == "unreviewed"
    assert controls.list_reviews("port", row["id"], conn)[0]["decision"] == "approved"


def test_findings_keep_ids_and_decisions(syn_env):
    c = syn_env["conn"]
    kwargs = {"conn": c, "processed_dir": syn_env["processed_dir"]}
    first = checks.run_checks("p1", syn_env["doc_id"], **kwargs)
    controls.review("finding", first[0].id, "rejected", "false alarm", "rev", "p1", c)
    again = checks.run_checks("p1", syn_env["doc_id"], **kwargs)
    assert [x.id for x in first] == [x.id for x in again]
    assert again[0].review_status == "rejected"


def test_rejected_and_missing_entities_excluded_from_reasoning(conn, dirs):
    _write(dirs, _sections([["CompA", "PortAbc", "Provided", "IX"]]))
    extract.extract_document("p1", "d1", conn=conn, processed_dir=dirs["processed_dir"], use_diagram=False)
    row = extract.get_inventory("p1", "d1", conn=conn)["ports"][0]
    controls.review("port", row["id"], "approved", "ok", "rev", "p1", conn)
    _write(dirs, _sections([]))
    extract.extract_document("p1", "d1", conn=conn, processed_dir=dirs["processed_dir"], use_diagram=False)
    assert not extract.get_inventory("p1", "d1", conn=conn)["ports"][0]["extraction_present"]
    assert not extract.get_inventory("p1", "d1", conn=conn, include_rejected=False)["ports"]


def test_llm_quote_must_identify_entity_in_current_chunk():
    provider = MockProvider([{"components": [{"name": "FrontWiper", "evidence": "The WiperManager is the core component"}]}])
    assert extract.extract_from_prose(sec(), "d", "1", provider=provider) == []


def test_llm_name_substrings_and_invented_descriptions_are_not_facts():
    quote = "The WiperManager is the core component"
    provider = MockProvider([{"components": [{"name": "Wiper", "evidence": quote},
        {"name": "WiperManager", "description": "Launches a rocket", "evidence": quote}]}])
    rows = extract.extract_from_prose(sec(), "d", "1", provider=provider)
    assert len(rows) == 1 and rows[0].name == "WiperManager"
    assert rows[0].description == quote


def test_prose_schema_rejects_missing_and_empty_evidence():
    from pydantic import ValidationError
    for item in ({"name": "ControllerA"}, {"name": "ControllerA", "evidence": ""}):
        with pytest.raises(ValidationError):
            extract.ProseExtraction.model_validate({"components": [item]})


def test_prose_functional_component_preserves_page_evidence():
    doc = {"doc_id": "d", "version": "1", "sections": [{"section_id": "2", "title": "Components",
        "page_start": 1, "page_end": 2, "text": "LevelSensor senses fluid in the tank. LampManager provides the core functionality.",
        "page_texts": {"1": "LevelSensor senses fluid in the tank.", "2": "LampManager provides the core functionality."}, "tables": []}]}
    rows = extract.extract_entities(doc, use_diagram=False, use_convention=False)["components"]
    assert {(r.name, r.sources[0].page) for r in rows} == {("LevelSensor", 1), ("LampManager", 2)}


def test_llm_page_follows_quote_not_section_start():
    quote = "The WiperManager is the core component"
    section = {**sec(), "page_start": 3, "page_end": 7, "page_texts": {"3": "Other material", "7": TEXT}}
    provider = MockProvider([{"components": [{"name": "WiperManager", "evidence": quote}]}])
    row = extract.extract_from_prose(section, "d", "1", provider=provider)[0]
    assert row.sources[0].page == 7


def test_continued_table_rows_cite_their_own_page():
    table = {"kind": "table", "header": ["Component", "Port", "Direction", "Interface"],
             "rows": [["CtrlA", "PortFirst", "provided", "IA"], ["CtrlB", "PortNext", "required", "IA"]],
             "row_pages": [4, 5]}
    rows = extract.entities_from_table(table, extract.Source(doc_id="d", page=4))
    assert [r.sources[0].page for r in rows] == [4, 5]
    assert "CtrlB | PortNext" in rows[1].sources[0].evidence


@pytest.mark.parametrize("claim", ["MotorDriver requires MotorCommand.",
                                  "OtherDriver provides MotorCommand.",
                                  "MotorDriver does not provide MotorCommand."])
def test_relationship_reversal_and_negation_are_unsupported(claim):
    assert not qa.sentence_supported(claim, "MotorDriver provides MotorCommand.")


def test_unsupported_cited_sentence_removed():
    lmap = {"C1": {"text": "MotorDriver provides MotorCommand.", "metadata": {}}}
    clean, labs, dropped = qa.supported_only(
        "MotorDriver provides MotorCommand [C1]. MotorDriver requires MotorCommand [C1].", lmap)
    assert "requires" not in clean and "provides" in clean and labs == ["C1"] and dropped == 1


def test_receiver_cannot_be_swapped_with_another_named_component():
    source = "CtrlA sends MotorCommand to DriverB. SensorC monitors DriverB."
    assert qa.sentence_supported("DriverB receives MotorCommand from CtrlA.", source)
    assert not qa.sentence_supported("CtrlA sends MotorCommand to SensorC.", source)
    assert not qa.sentence_supported("DriverB sends MotorCommand to CtrlA.", source)


def test_required_port_of_owner_checks_direction():
    source = "CtrlA provides PortAlpha."
    assert qa.sentence_supported("The provided port PortAlpha of CtrlA.", source)
    assert not qa.sentence_supported("The required port PortAlpha of CtrlA.", source)


def test_port_glyphs_use_geometry_not_misleading_names():
    import pymupdf
    pdf = pymupdf.open()
    page = pdf.new_page()
    shape = page.new_shape()
    shape.draw_polyline([(91, 156), (95, 160), (91, 164)])
    shape.finish(color=(0, 0, 0))
    shape.draw_polyline([(256, 176), (256, 184), (258, 184), (262, 180), (258, 176), (256, 176)])
    shape.finish(color=(0, 0, 0), fill=(1, 1, 1))
    shape.commit()
    glyphs = diagram._port_glyphs(page)
    box = diagram.Box(0, (100, 100, 250, 250), title="ControllerA")
    required = diagram.PortLabel("PpMisleading", diagram.Line("PpMisleading", (105, 156, 150, 164)),
                                 box, "left", 0, True)
    provided = diagram.PortLabel("RpMisleading", diagram.Line("RpMisleading", (200, 176, 245, 184)),
                                 box, "right", 0, True)
    assert diagram._glyph_direction(required, glyphs) == "required"
    assert diagram._glyph_direction(provided, glyphs) == "provided"
    assert diagram._glyph_direction(required, []) == "unknown"
    pdf.close()


def test_correction_api_roles_validation_and_scope(client, tmp_path, make_pdf):
    assert _upload(client, "admin", make_pdf(tmp_path / "edit.pdf")).status_code == 200
    rows = client.get("/projects/p1/inventory", headers=H("rev")).json()
    row = rows["components"][0]
    payload = {"item_type": "component", "item_id": row["id"], "changes": {"description": "Reviewed source description"},
               "comment": "Verified against the source"}
    assert client.post("/projects/p1/corrections", headers=H("view"), json=payload).status_code == 403
    assert client.post("/projects/p2/corrections", headers=H("rev"), json=payload).status_code == 422
    assert client.post("/projects/p1/corrections", headers=H("rev"), json=payload).status_code == 200
    row = client.get("/projects/p1/inventory", headers=H("view")).json()["components"][0]
    assert row["description"] == "Reviewed source description" and row["review_status"] == "corrected"
    payload["changes"] = {"project_id": "p2"}
    assert client.post("/projects/p1/corrections", headers=H("rev"), json=payload).status_code == 422
    coverage = client.get("/projects/p1/coverage", params={"doc_id": "edit"}, headers=H("view")).json()
    assert coverage["completeness"] == "not_measured" and coverage["warnings"]


def test_uploaded_diagram_inventory_survives_reextract(client, diagram_pdf):
    assert _upload(client, "admin", diagram_pdf).status_code == 200
    before = client.get("/projects/p1/inventory", headers=H("admin"), params={"doc_id": "d"}).json()
    assert len(before["ports"]) >= 4
    response = client.post("/projects/p1/extract", headers=H("admin"), json={"doc_id": "d", "use_llm": False})
    assert response.status_code == 200
    after = client.get("/projects/p1/inventory", headers=H("admin"), params={"doc_id": "d"}).json()
    assert [(p["name"], p["component"], p["direction"]) for p in after["ports"]] == [
        (p["name"], p["component"], p["direction"]) for p in before["ports"]]


def test_token_auth_prevents_impersonation_and_cookie_tampering(client, monkeypatch):
    secret = "example-test-token-with-enough-entropy"
    monkeypatch.setenv("HLD_AUTH_TOKEN_HASHES", json.dumps({"view": hashlib.sha256(secret.encode()).hexdigest()}))
    assert client.get("/projects", headers=H("admin")).status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    response = client.post("/login", json={"token": secret})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    client.cookies.set("hld_user", "admin")
    assert client.get("/me", headers=H("admin")).json()["user"] == "view"
    assert client.post("/projects/p1/run-checks", headers=H("admin")).status_code == 403
    assert client.post("/logout").status_code == 200
    assert client.get("/projects").status_code == 401
    assert client.get("/projects", headers={"Authorization": "Bearer " + secret}).status_code == 200


def test_invalid_auth_configuration_fails_closed(client, monkeypatch):
    monkeypatch.setenv("HLD_AUTH_TOKEN_HASHES", "{}")
    assert client.get("/projects", headers=H("admin")).status_code == 503


def test_reachable_runtime_without_models_is_not_answer_ready(client, monkeypatch):
    monkeypatch.setattr(api.llm, "ollama_status", lambda: {"reachable": True, "models": []})
    health = client.get("/health").json()
    assert health["qa_ready"] is False and api.settings.demo_model in health["missing_models"]


def test_field_aware_audit_requires_one_correct_owner_and_direction():
    from scripts.audit_extraction import field_aware_score
    gold = [{"doc_id": "d", "type": "port", "name": "PortA", "component": "OwnerA", "direction": "provided"}]
    inv = {"d": {"ports": [{"name": "PortA", "component": "OwnerB", "direction": "provided"},
                              {"name": "PortA", "component": "OwnerA", "direction": "required"}]}}
    assert field_aware_score(gold, inv, lambda a, b: a == b)["port"]["joint_matches"] == 0


def test_field_aware_signal_reference_checks_receiver_endpoint():
    from scripts.audit_extraction import field_aware_score
    gold = [{"doc_id": "d", "type": "signal", "name": "SignalA", "component": "ReceiverA", "direction": "required"}]
    inv = {"d": {"signals": [{"name": "SignalA", "sender": "ReceiverA", "receivers": ["WrongReceiver"]}]}}
    assert field_aware_score(gold, inv, lambda a, b: a == b)["signal"]["joint_matches"] == 0
    inv["d"]["signals"][0]["receivers"] = ["ReceiverA"]
    assert field_aware_score(gold, inv, lambda a, b: a == b)["signal"]["joint_matches"] == 1


def test_concurrent_health_and_warmup_load_one_embedder(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from app import retrieve
    loaded = []
    class FakeBge:
        name = "bge"
        def __init__(self, _):
            loaded.append(self)
            time.sleep(0.03)
    monkeypatch.delenv("EMBEDDER", raising=False)
    monkeypatch.setattr(retrieve, "BgeEmbedder", FakeBge)
    retrieve.reset_embedder()
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: retrieve.get_embedder(), range(8)))
    assert len(loaded) == 1 and len({id(value) for value in results}) == 1


def test_rejected_entity_is_not_a_release_addition(two):
    from app.compare import compare
    _ent(two, "p1", "b", "2.0", "components", "RejectedGhost")
    item_id = two.execute("SELECT id FROM components WHERE name='RejectedGhost'").fetchone()[0]
    controls.review("component", item_id, "rejected", "Not an architecture element", "rev", "p1", two)
    diff = compare("p1", "a", "1.0", "b", "2.0", conn=two)
    assert not diff.components.added
