"""Ask page: 3-letter all-caps component names, silent sample-question checks, Refused/Answered in the session
list, compact inventory tables and question verbs kept out of the "terms not found" note."""
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app import controls, extract, qa, retrieve

ROOT = Path(__file__).resolve().parent.parent
NAMES = {"acc": "ACC", "accesskeyless": "AccessKeyless", "epb": "EPB", "vehiclestate": "VehicleState"}


# ---------------------------------------------------------------- short all-caps component names
@pytest.mark.parametrize("question, expected", [
    ("Which ports does the ACC have?", ["acc"]),
    ("Which signals does ACC send to the EPB?", ["acc", "epb"]),
    ("Which ports does the ACC's controller have?", ["acc"]),
    ("Is access to the vehicle logged?", []),              # 'acc' inside a word
    ("which ports does the acc have?", []),               # not written in capitals
    ("Which ports does the AccessKeyless have?", ["accesskeyless"]),
    ("What does ACCX do?", []),                           # longer all-caps word
])
def test_short_all_caps_names_match_as_whole_words(question, expected):
    assert sorted(retrieve.match_components(question, NAMES)) == sorted(expected)


def test_short_names_need_the_display_name():
    assert retrieve.match_components("Which ports does the ACC have?", list(NAMES)) == []  # plain names: as before


def test_mixed_case_three_letter_names_do_not_match():
    assert retrieve.match_components("What does the Hmi do?", {"hmi": "Hmi"}) == []


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(controls.SCHEMA)
    extract.ensure_schema(c)
    return c


def _add(c, table, name, page, project="p", **fields):
    c.execute(f"INSERT INTO {table}(project_id, doc_id, version, name, norm_name, fields, page, section, "
              "sources_json, extraction_method, confidence, review_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (project, "chassis_r1", "1.0", name, extract.normalize_name(name), json.dumps(fields), page, "3.2 ACC",
               "[]", "table", "High", "unreviewed"))


def _chassis(c, project="p"):
    _add(c, "components", "ACC", 7, project=project)
    _add(c, "ports", "RpVehSpeed", 7, project=project, component="ACC", direction="required", interface="IVehSpeed")
    _add(c, "ports", "PpAccTorque", 7, project=project, component="ACC", direction="provided", interface="ITorque")
    return c


def test_inventory_answer_for_a_three_letter_component():
    c = _chassis(_db())
    q = "Which ports does the ACC have?"
    hits = qa.inventory_hits("p", q, c)
    info = retrieve.analyze_query(q, [], retrieve.inventory_names(extract.get_inventory("p", conn=c)))
    text, labels, blocks, _ = qa.structured_blocks(q, info, qa.label_map(hits))
    assert text.startswith("ACC (chassis_r1, v1.0, p.7 §3.2) has 1 required port: RpVehSpeed (IVehSpeed); "
                           "1 provided port: PpAccTorque (ITorque) [C1].")
    assert [g["title"] for g in blocks[0]["groups"]] == ["Required", "Provided"]


# ---------------------------------------------------------------- terms not found
def test_question_verbs_are_not_reported_as_missing_terms():
    terms = retrieve.tokenize("Who sends WiperSpeedX, receives it, has, provides or requires it, and is it sent?")
    shown = retrieve.reportable_missing(terms)
    assert not {"send", "sent", "receive", "provide", "provides", "require", "requires", "have"} & set(shown)
    assert "wiperspeedx" in shown


def test_coverage_score_is_unchanged_by_the_note_filter(monkeypatch):
    class Bm25:
        idf = {"wiper": 1.0}

    class Corpus:
        bm25 = Bm25()

    cov, missing = retrieve.term_coverage("Who sends the wiper flag?", Corpus())
    assert "send" in missing and cov == pytest.approx(1 / 3)  # the score still counts the verb
    assert retrieve.reportable_missing(missing) == ["flag"]


# ---------------------------------------------------------------- silent sample checks (API)
class _Ret:
    def __init__(self, hits):
        self.hits, self.top_score, self.on_topic, self.exact_identifier = hits, 0.9, bool(hits), False
        self.coverage, self.missing_terms, self.searched, self.release_note = 1.0, [], ["chassis_r1"], ""
        self.query = None


@pytest.fixture
def client(tmp_path, conn, dirs, monkeypatch):
    from fastapi.testclient import TestClient

    from app import api
    extract.ensure_schema(conn)
    _chassis(conn, project="p1")
    conn.commit()
    conn.close()

    def fake_retrieve(project_id, question, conn=None, **_):
        r = _Ret(qa.inventory_hits(project_id, question, conn))
        r.query = retrieve.analyze_query(question, [], retrieve.inventory_names(extract.get_inventory(project_id,
                                                                                                        conn=conn)))
        return r

    monkeypatch.setattr(qa.retrieve, "retrieve", fake_retrieve)
    monkeypatch.setattr(qa.retrieve, "embedder_status", lambda: {"fallback": False})
    api.app.dependency_overrides[api.get_paths] = lambda: api.Paths(tmp_path / "test.db", dirs["chroma_dir"],
                                                                     dirs["processed_dir"])
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _rows(tmp_path, sql):
    db = sqlite3.connect(tmp_path / "test.db")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def test_check_answers_without_storing_and_is_audited(client, tmp_path):
    q = "Which ports does the ACC have?"
    a = client.post("/projects/p1/ask", json={"question": q, "check": True}, headers={"X-User": "view"}).json()
    assert a["mode"] == "generated" and a["answer_type"] == "inventory_list" and a["id"] is None
    assert _answers(tmp_path) == 0
    assert [r[0] for r in _rows(tmp_path, "SELECT action FROM audit_log WHERE action LIKE 'ask%'")] == ["ask_check"]
    b = client.post("/projects/p1/ask", json={"question": q}, headers={"X-User": "view"}).json()
    assert b["id"] is not None and _answers(tmp_path) == 1


def _answers(tmp_path) -> int:
    """Stored answer rows (the table is created with the first stored answer)."""
    if not _rows(tmp_path, "SELECT name FROM sqlite_master WHERE name='answers'"):
        return 0
    return _rows(tmp_path, "SELECT COUNT(*) FROM answers")[0][0]


def test_check_keeps_membership_and_project_isolation(client, tmp_path):
    q = "Which ports does the ACC have?"
    other = client.post("/projects/p2/ask", json={"question": q, "check": True}, headers={"X-User": "view"}).json()
    assert other["mode"] == "refused"  # p2 has no ACC: the sample would not be shown there
    db = controls.get_conn(tmp_path / "test.db")
    controls.add_user("outsider", "admin", conn=db)
    db.close()
    r = client.post("/projects/p1/ask", json={"question": q, "check": True}, headers={"X-User": "outsider"})
    assert r.status_code == 403


# ---------------------------------------------------------------- browser-side rules (node)
NODE = shutil.which("node")


def _node(script: str):
    js = (ROOT / "app/static/js/answer_view.js").as_uri()
    code = f"import * as v from {json.dumps(js)};\n{script}"
    out = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60,
                         check=True)
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(not NODE, reason="node not installed")


@needs_node
def test_evidence_labels_distinguish_citations_from_weak_or_missing_support():
    got = _node("""console.log(JSON.stringify([
      v.evidenceLabel({mode: "generated", confidence: "Medium", citations: [{}]}),
      v.evidenceLabel({mode: "generated", confidence: "Medium", citations: [{}, {}]}),
      v.evidenceLabel({mode: "generated", confidence: "Low", citations: [{}]}),
      v.evidenceLabel({mode: "generated", confidence: "High", citations: []}),
      v.evidenceLabel({mode: "refused", confidence: "Low", citations: [{}]}),
      v.evidenceLabel({mode: "evidence_only", confidence: "Low", citations: [{}]}),
      v.evidenceLabel({mode: "generated", built_from: "inventory", confidence: "Medium", citations: [{}]}),
      v.evidenceLabel({mode: "generated", built_from: "inventory", confidence: "High", citations: [{}]})
    ]));""")
    assert got == ["Based on one cited passage", "Based on 2 cited passages", "Limited evidence",
                   "No cited evidence", "Answer withheld", "Retrieved passages only",
                   "Extraction needs verification", "Facts corroborated"]


@needs_node
def test_every_session_entry_says_refused_or_answered():
    got = _node("""console.log(JSON.stringify(["refused", "generated", "evidence_only"].map((mode) => v.statusText({mode}))));""")
    assert got == ["Refused", "Answered", "Answered"]


@needs_node
def test_table_layout_drops_empty_page_column_and_single_group_row():
    got = _node("""const cols = [["name", "Port"], ["direction", "Direction"], ["interface", "Interface"], ["page", "Page"]];
      const g = (rows) => ({columns: cols, rows});
      console.log(JSON.stringify([
        v.tableLayout([g([{page: null}, {page: null}])]),
        v.tableLayout([g([{page: null}]), g([{page: 15}])]),
      ]));""")
    one, two = got
    assert [c[0] for c in one["columns"]] == ["name", "direction", "interface"] and one["groupRows"] is False
    assert [c[0] for c in two["columns"]] == ["name", "direction", "interface", "page"] and two["groupRows"] is True


@needs_node
def test_sample_candidates_have_fallbacks_per_slot():
    inv = {"components": [{"name": "ACC", "section": "3.2 ACC"}, {"name": "EPB", "section": "3.3 EPB"},
                          {"name": "ESC", "section": ""}, {"name": "VLC"}],
           "ports": [{"component": "EPB"}, {"component": "EPB"}, {"component": "ACC"}, {"component": "ESC"}],
           "signals": [{"name": "TqReq", "sender": "ACC"}, {"name": "Orphan"}, {"name": "BrkReq", "sender": "EPB"}]}
    got = _node(f"console.log(JSON.stringify({{slots: v.sampleCandidates({json.dumps(inv)}), off: v.OFF_TOPIC}}));")
    assert got["slots"] == [
        {"kind": "ports", "questions": ["Which ports does the EPB have?", "Which ports does the ACC have?",
                                        "Which ports does the ESC have?"]},
        {"kind": "section", "questions": ["What does section 3.3 EPB describe?", "What does section 3.2 ACC describe?"]},
        {"kind": "signal", "questions": ["Who sends TqReq and who receives it?", "Who sends BrkReq and who receives it?"]}]
    assert got["off"] == "What will the weather be like tomorrow?"


@needs_node
def test_no_ports_question_when_no_component_has_ports():
    inv = {"components": [{"name": "ACC", "section": "4 Compositions"}], "ports": [], "signals": []}
    got = _node(f"console.log(JSON.stringify(v.sampleCandidates({json.dumps(inv)})));")
    assert got == [{"kind": "section", "questions": ["What does section 4 Compositions describe?"]}]


@needs_node
def test_extraction_gap_reply_does_not_count_as_an_answered_sample():
    got = _node("""const valid = {confidence: "High", citations: [{label: "C1"}], answer: "Supported text"};
      console.log(JSON.stringify([
      v.sampleAnswered("ports", {...valid, mode: "generated", answer_type: "inventory_list"}),
      v.sampleAnswered("ports", {mode: "generated", answer_type: "text"}),
      v.sampleAnswered("signal", {mode: "refused", answer_type: "text"}),
      v.sampleAnswered("section", {...valid, mode: "generated", answer_type: "text"}),
      v.sampleAnswered("section", {mode: "refused"}),
      v.sampleAnswered("history", {...valid, mode: "generated", confidence: "Medium"}),
      v.sampleAnswered("history", {...valid, mode: "generated", citations: []}),
      v.sampleAnswered("history", {...valid, mode: "generated", answer: ""})]));""")
    assert got == [True, False, False, True, False, False, False, False]


def test_ask_page_checks_samples_silently_without_deliberate_refusals():
    js = (ROOT / "app/static/js/ask.js").read_text(encoding="utf-8")
    assert "check: true" in js and "found.slice(0, 3)" in js and "OFF_TOPIC" not in js
    assert "statusText(item.answer)" in js and '"Refused") : null' not in js
    for f in [*(ROOT / "app/templates").glob("*.html"), *(ROOT / "app/static/js").glob("*.js")]:
        assert not re.search(r"https?://", f.read_text(encoding="utf-8")), f.name
