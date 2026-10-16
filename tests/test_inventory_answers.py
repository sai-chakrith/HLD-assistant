"""Inventory-backed list answers: compact text cited once per list, grouped rows for the table view, honest labels
(no model name when no model wrote the text) and the inventory confidence rule."""
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app import controls, extract, qa, retrieve

ROOT = Path(__file__).resolve().parent.parent


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(controls.SCHEMA)  # audit log for ask()
    extract.ensure_schema(c)
    return c


def _add(c, table, name, page, method="diagram", conf="High", project="p", doc="doc1", sources=None, **fields):
    c.execute(f"INSERT INTO {table}(project_id, doc_id, version, name, norm_name, fields, page, section, "
              "sources_json, extraction_method, confidence, review_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (project, doc, "R3.2.3", name, extract.normalize_name(name), json.dumps(fields), page, "4.3 Interior",
               json.dumps(sources or []), method, conf, "unreviewed"))


PORTS = ["DashBoardBrightness", "HMI", "OutsideBrightness", "LowBeamInd", "DisplayParkingLight", "CHLHome", "State",
         "MechKey", "AccessKeyless", "Access", "RemoteKey", "CarFinder", "GlobalDimLight", "DriverProfile", "Crash"]


def _seed(method="diagram", conf="High", extra_page=None):
    c = _db()
    _add(c, "components", "InteriorLightManager", 14, method=method)
    for i, n in enumerate(PORTS):
        page = extra_page if (extra_page and i == len(PORTS) - 1) else 14
        _add(c, "ports", n, page, method=method, conf=conf, component="InteriorLightManager", direction="required")
    _add(c, "ports", "PpLight", 14, method=method, conf=conf, component="InteriorLightManager", direction="provided",
         interface="ILight")
    return c


def _blocks(c, question, extra_hits=()):
    hits = qa.inventory_hits("p", question, c) + list(extra_hits)
    inv = extract.get_inventory("p", conn=c)
    info = retrieve.analyze_query(question, [], retrieve.inventory_names(inv))
    lmap = qa.label_map(hits)
    return qa.structured_blocks(question, info, lmap), lmap


Q = "Which ports does the InteriorLightManager have?"


# ---------------------------------------------------------------- text: cite once per claim
def test_text_cites_once_per_distinct_page_and_not_per_item():
    (text, labels, blocks, rows), _ = _blocks(_seed(), Q)
    assert len(re.findall(r"\[C\d+(?:, C\d+)*\]", text)) <= len({r["page"] for r in rows})
    assert "(required, p." not in text and "diagram" not in text
    assert text.count("p.14") == 1  # the shared page is said once, in the header
    assert text.startswith("InteriorLightManager (doc1, R3.2.3, p.14 §4.3) has 15 required ports: DashBoardBrightness, ")
    assert "1 provided port: PpLight (ILight)" in text and text.endswith("[C1].")
    assert all(p in text for p in PORTS)


def test_long_lists_are_not_truncated():
    c = _seed()
    for i in range(7):
        _add(c, "ports", f"PpExtra{i}", 14, component="InteriorLightManager", direction="provided")
    (text, _, blocks, rows), _ = _blocks(c, Q)
    assert len(rows) == 23 and [g["count"] for g in blocks[0]["groups"]] == [15, 8]
    assert "8 provided ports:" in text and "PpExtra6" in text


def test_row_on_another_page_carries_its_page_once():
    (text, _, blocks, _), _ = _blocks(_seed(extra_page=15), Q)
    assert "Crash p.15" in text and text.count("p.14") == 1
    req = blocks[0]["groups"][0]
    assert req["page"] == 14
    assert [r["page"] for r in req["rows"] if r["name"] == "Crash"] == [15]
    assert all(r["page"] is None for r in req["rows"] if r["name"] != "Crash")


def test_blocks_are_grouped_by_direction_with_columns():
    (_, _, blocks, _), _ = _blocks(_seed(), Q)
    b = blocks[0]
    assert (b["subject"], b["doc_id"], b["version"], b["page"], b["section_id"]) == (
        "InteriorLightManager", "doc1", "R3.2.3", 14, "4.3")
    assert [(g["title"], g["count"]) for g in b["groups"]] == [("Required", 15), ("Provided", 1)]
    assert [c[1] for c in b["groups"][0]["columns"]] == ["Port", "Direction", "Interface", "Page"]
    assert b["groups"][1]["rows"][0] == {"name": "PpLight", "direction": "provided", "interface": "ILight",
                                         "page": None, "method": "diagram", "confidence": "High"}


def test_interfaces_question_names_missing_interfaces_once():
    c = _seed()
    (text, _, _, _), _ = _blocks(c, "Which interfaces does the InteriorLightManager require?")
    assert "15 required ports with no interface extracted: DashBoardBrightness" in text
    assert text.count("no interface extracted") == 1 and "ILight" not in text


def test_signals_grouped_sent_and_received():
    c = _seed()
    _add(c, "signals", "LightCmd", 14, sender="InteriorLightManager", receivers=["LampDrv"])
    _add(c, "signals", "DoorOpen", 14, sender="DoorCtrl", receivers=["InteriorLightManager"])
    (text, _, blocks, _), _ = _blocks(c, "Which signals does the InteriorLightManager use?")
    titles = [g["title"] for g in blocks[0]["groups"]]
    assert "Sent" in titles or "Received" in titles
    assert text.count("[C1]") == 1


# ---------------------------------------------------------------- confidence rule
def test_diagram_only_rows_are_medium_with_reason():
    (_, _, _, rows), lmap = _blocks(_seed(), Q)
    assert qa.inventory_confidence(rows, lmap, "ports") == ("Medium", "all ports read from a diagram, with no second source")


def test_any_medium_row_caps_at_medium():
    c = _seed(method="table")
    _add(c, "ports", "Weak", 14, method="table", conf="Medium", component="InteriorLightManager", direction="required")
    (_, _, _, rows), lmap = _blocks(c, Q)
    conf, why = qa.inventory_confidence(rows, lmap, "ports")
    assert conf == "Medium" and "Medium extraction confidence" in why


def test_table_rows_with_high_confidence_are_high():
    (_, _, _, rows), lmap = _blocks(_seed(method="table"), Q)
    assert qa.inventory_confidence(rows, lmap, "ports") == ("High", "all ports read from a table with High confidence")


def _text_hit(page, chunk_type="text"):
    return {"id": f"doc1::x::{page}", "score": 0.8,
            "metadata": {"doc_id": "doc1", "page_start": page, "page_end": page, "chunk_type": chunk_type},
            "text": "The InteriorLightManager reads " + ", ".join(PORTS) + " and provides PpLight."}


def test_diagram_rows_confirmed_by_text_on_another_page_are_high():
    (_, _, _, rows), lmap = _blocks(_seed(), Q, extra_hits=[_text_hit(20)])
    assert qa.inventory_confidence(rows, lmap, "ports")[0] == "High"


def test_diagram_rows_confirmed_by_a_table_chunk_are_high():
    (_, _, _, rows), lmap = _blocks(_seed(), Q, extra_hits=[_text_hit(14, "table")])
    assert qa.inventory_confidence(rows, lmap, "ports")[0] == "High"


@pytest.mark.parametrize("hit", [_text_hit(14, "figure_text"), _text_hit(20, "figure_text"), _text_hit(14)],
                         ids=["figure-text-same-page", "figure-text-other-page", "text-same-page"])
def test_the_diagrams_own_text_is_not_a_second_source(hit):
    (_, _, _, rows), lmap = _blocks(_seed(), Q, extra_hits=[hit])
    assert qa.inventory_confidence(rows, lmap, "ports") == ("Medium", "all ports read from a diagram, with no second source")


def test_diagram_rows_with_second_extraction_method_are_high():
    c = _db()
    for n in ("RpA", "RpB"):
        _add(c, "ports", n, 14, method="diagram", component="InteriorLightManager", direction="required",
             sources=[{"page": 14, "method": "diagram"}, {"page": 14, "method": "llm"}])
    (_, _, _, rows), lmap = _blocks(c, Q)
    assert qa.inventory_confidence(rows, lmap, "ports")[0] == "High"


def test_convention_is_not_a_second_source():
    c = _db()
    _add(c, "ports", "RpA", 14, method="diagram", component="InteriorLightManager", direction="required",
         sources=[{"page": 14, "method": "diagram"}, {"page": 14, "method": "convention"}])
    (_, _, _, rows), lmap = _blocks(c, Q)
    assert qa.inventory_confidence(rows, lmap, "ports")[0] == "Medium"


# ---------------------------------------------------------------- ask(): labels, model name, API shape
class _Ret:
    def __init__(self, hits):
        self.hits, self.top_score, self.on_topic, self.exact_identifier = hits, 0.9, True, False
        self.coverage, self.missing_terms, self.searched, self.release_note = 1.0, [], ["doc1"], ""
        self.query = None


class _Fake:
    model = "fake-model"

    def __init__(self, reply):
        self.reply = reply

    def generate(self, prompt, system="", model=None, purpose="", chunk_ids=None):
        return self.reply


def _ask(monkeypatch, c, question, extra_hits=(), reply=""):
    hits = qa.inventory_hits("p", question, c) + list(extra_hits)
    inv = extract.get_inventory("p", conn=c)
    ret = _Ret(hits)
    ret.query = retrieve.analyze_query(question, [], retrieve.inventory_names(inv))
    monkeypatch.setattr(qa.retrieve, "retrieve", lambda *a, **k: ret)
    monkeypatch.setattr(qa.retrieve, "embedder_status", lambda: {"fallback": False})
    monkeypatch.setattr(controls, "require_role", lambda *a, **k: None)
    return qa.ask("p", question, "admin", conn=c, provider=_Fake(reply))


def test_inventory_only_answer_has_no_model_name(monkeypatch):
    a = _ask(monkeypatch, _seed(), Q)
    assert a.answer_type == "inventory_list" and a.built_from == "inventory" and a.model == ""
    assert a.confidence == "Medium" and "diagram" in a.confidence_reason
    assert a.inventory["blocks"][0]["groups"][0]["count"] == 15
    ev = next(e for e in a.evidence if e.get("inventory"))
    assert ev["inventory_subject"] == "InteriorLightManager" and ev["inventory_counts"] == {"ports": 16, "signals": 0}


def test_model_named_when_it_wrote_the_description(monkeypatch):
    text = {"id": "doc1::4.3::0", "score": 0.8, "metadata": {"doc_id": "doc1", "page_start": 13},
            "text": "The InteriorLightManager switches the cabin lamps on and off."}
    a = _ask(monkeypatch, _seed(), Q, extra_hits=[text],
             reply="The InteriorLightManager switches the cabin lamps on and off [C2].")
    assert a.built_from == "inventory+llm" and a.model == "fake-model"
    assert a.inventory["description"].endswith("[C2].") and a.answer.endswith("[C2].")


def test_llm_answer_keeps_model_name(monkeypatch):
    text = {"id": "doc1::4.3::0", "score": 0.9, "metadata": {"doc_id": "doc1", "page_start": 13},
            "text": "The InteriorLightManager switches the cabin lamps on and off."}
    a = _ask(monkeypatch, _seed(), "What does the InteriorLightManager do?", extra_hits=[text],
             reply="The InteriorLightManager switches the cabin lamps [C1].")
    assert a.answer_type == "text" and a.built_from == "llm" and a.model == "fake-model"


@pytest.fixture
def client(tmp_path, conn, dirs, monkeypatch):
    from fastapi.testclient import TestClient

    from app import api
    extract.ensure_schema(conn)
    for i, n in enumerate(PORTS):
        _add(conn, "ports", n, 14, project="p1", component="InteriorLightManager", direction="required")
    conn.commit()
    conn.close()

    def fake_retrieve(project_id, question, conn=None, **_):  # project-scoped inventory evidence only
        r = _Ret(qa.inventory_hits(project_id, question, conn))
        r.on_topic = bool(r.hits)
        r.query = retrieve.analyze_query(question, [], retrieve.inventory_names(extract.get_inventory(project_id, conn=conn)))
        return r

    monkeypatch.setattr(qa.retrieve, "retrieve", fake_retrieve)
    monkeypatch.setattr(qa.retrieve, "embedder_status", lambda: {"fallback": False})
    api.app.dependency_overrides[api.get_paths] = lambda: api.Paths(tmp_path / "test.db", dirs["chroma_dir"],
                                                                     dirs["processed_dir"])
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def test_api_returns_grouped_rows_and_keeps_viewer_and_project_isolation(client, tmp_path):
    r = client.post("/projects/p1/ask", json={"question": Q}, headers={"X-User": "view"})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["answer_type"] == "inventory_list" and a["built_from"] == "inventory" and a["model"] == ""
    assert a["confidence"] == "Medium" and a["confidence_reason"]
    assert [(g["title"], g["count"]) for g in a["inventory"]["blocks"][0]["groups"]] == [("Required", 15)]
    other = client.post("/projects/p2/ask", json={"question": Q}, headers={"X-User": "view"}).json()
    assert other["mode"] == "refused" and other["inventory"] is None
    assert not any(p in json.dumps(other) for p in PORTS)
    db = controls.get_conn(tmp_path / "test.db")
    controls.add_user("outsider", "admin", conn=db)
    db.close()
    assert client.post("/projects/p1/ask", json={"question": Q}, headers={"X-User": "outsider"}).status_code == 403


# ---------------------------------------------------------------- browser-side display rules (node)
NODE = shutil.which("node")


def _node(script: str):
    js = (ROOT / "app/static/js/answer_view.js").as_uri()
    code = f"import * as v from {json.dumps(js)};\n{script}"
    out = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60,
                         check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_label_and_model_name_rules():
    got = _node("""console.log(JSON.stringify([
      [v.sourceText({mode: "generated", built_from: "inventory"}), v.showsModel({mode: "generated", built_from: "inventory", model: "m"})],
      [v.sourceText({mode: "generated", built_from: "inventory+llm"}), v.showsModel({mode: "generated", built_from: "inventory+llm", model: "m"})],
      [v.sourceText({mode: "generated", built_from: "llm"}), v.showsModel({mode: "generated", built_from: "llm", model: "m"})],
      [v.sourceText({mode: "generated", built_from: "change_history"}), v.showsModel({mode: "generated", built_from: "change_history", model: "m"})],
      [v.sourceText({mode: "refused"}), v.showsModel({mode: "refused", model: "m"})]]));""")
    assert got == [["Built from extracted inventory", False],
                   ["Built from extracted inventory; description written by the model", True],
                   ["Generated answer", True], ["Built from the change-history table", False], ["Refused", False]]


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_repeated_question_replaces_the_session_entry():
    got = _node("""const l = [];
      v.remember(l, "Which ports does X have?", 1);
      v.remember(l, "  which ports does  X have? ", 2);
      v.remember(l, "Other?", 3);
      v.remember(l, "Which ports does X have?", 4);
      for (let i = 0; i < 6; i++) v.remember(l, "q" + i, i);
      const m = []; v.remember(m, "A", 1); v.remember(m, "A", 2);
      console.log(JSON.stringify({m, top: l.map((x) => x.question), n: l.length}));""")
    assert got["m"] == [{"question": "A", "answer": 2}]
    assert got["n"] == 5 and got["top"] == ["q5", "q4", "q3", "q2", "q1"]
    early = _node("""const l = []; v.remember(l, "A", 1); v.remember(l, "B", 2); v.remember(l, "A", 3);
      console.log(JSON.stringify(l.map((x) => x.question)));""")
    assert early == ["A", "B", "A"]  # only a repeat of the latest question is merged


def test_ask_page_does_not_print_raw_inventory_text_or_per_row_methods():
    js = (ROOT / "app/static/js/ask.js").read_text(encoding="utf-8")
    assert "Extracted inventory: " in js and "/architecture?" in js
    assert "methodText" not in js  # no method text inside the table rows
    for f in [*(ROOT / "app/templates").glob("*.html"), *(ROOT / "app/static/js").glob("*.js")]:
        assert not re.search(r"https?://", f.read_text(encoding="utf-8")), f.name
