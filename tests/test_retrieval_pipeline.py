"""Hybrid retrieval: identifier tokenisation, version detection, RRF, BM25 exact identifiers, release policy,
inventory evidence, section expansion, project isolation."""
import json
import sqlite3

import pytest

from app import extract, qa, retrieve as r
from app.chunk import chunk_document
from app.retrieve import RetrievalConfig, index_chunks


# ---------------------------------------------------------------- tokenisation
def test_tokenize_keeps_identifiers_whole_and_split():
    t = r.tokenize("The CmdWiperSpeed and Cmd_Wiper_Speed2 ports")
    assert "cmdwiperspeed" in t and {"cmd", "wiper", "speed"} <= set(t)
    assert "cmd_wiper_speed2" in t
    assert "the" not in t and "port" in t  # stop word dropped, plural stemmed
    assert "r3.2.3" in r.tokenize("release R3.2.3")


@pytest.mark.parametrize("word,expected", [("CmdWiperSpeed", True), ("snake_case_id", True), ("IDoorPos", True),
                                           ("CAN2", True), ("Wiper", False), ("HMI", False), ("R3.1", False),
                                           ("v2", False), ("Which", False)])
def test_is_identifier(word, expected):
    assert r.is_identifier(word) is expected


def test_identifiers_in_question_and_exact_match():
    assert r.identifiers_in("Who sends StaMirrorPos to the MirrorAdjust Manager?") == ["StaMirrorPos", "MirrorAdjust"]
    assert r.contains_identifier("port StaMirrorPos (out)", "stamirrorpos")
    assert not r.contains_identifier("port StaMirrorPosition", "StaMirrorPos")


# ---------------------------------------------------------------- versions
@pytest.mark.parametrize("q,versions,pref", [
    ("What does R3.1.5 say about wipers?", ["R3.1.5"], None),
    ("In R25-11, which interfaces exist?", ["R25-11"], None),
    ("In version 2.0 of the HLD, who receives MotorCurrent?", ["2.0"], None),
    ("In the latest release, what changed?", [], "latest"),
    ("What did the older document say?", [], "older"),
    ("Which ports does the WiperWasher have?", [], None),
])
def test_detect_versions(q, versions, pref):
    assert r.detect_versions(q) == (versions, pref)


def test_version_matching_and_families():
    assert r.version_matches("R3.2", "R3.2.3") and r.version_matches("2.0", "2") and r.version_matches("v1", "1.0")
    assert not r.version_matches("R3.1.5", "R3.2.3") and not r.version_matches("2.0", "1.0")
    assert r.doc_family("Window Lift ECU HLD v1.0", "a") == r.doc_family("Window Lift ECU HLD v2.0", "b")
    assert r.doc_family(None, "bodycomfort_r3.1.5") == r.doc_family(None, "bodycomfort_r3.2.3")
    assert r.doc_family("Seat ECU v3 (held-out)", "x") != r.doc_family("Door ECU v4 (held-out)", "y")


@pytest.mark.parametrize("q,qtype", [
    ("Which ports does the AutoWipingEvaluator have?", "list_ports"),
    ("Which component sends ReqCarFinder?", "who_provides"),
    ("What changed between R3.1.5 and R3.2.3?", "what_changed"),
    ("What is the purpose of the document?", "definition"),
    ("In the latest release, what is the newest change history entry?", "definition"),
])
def test_question_type(q, qtype):
    assert r.question_type(q) == qtype


# ---------------------------------------------------------------- fusion
def test_rrf_merge():
    fused = r.rrf([["a", "b", "c"], ["c", "a", "d"]], k=60)
    assert [x for x, _ in fused][:2] == ["a", "c"]  # in both lists near the top
    assert dict(fused)["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert [x for x, _ in r.rrf([["a", "b"], ["b"]], k=60, weights=[1.0, 3.0])][0] == "b"


def test_bm25_prefers_rare_exact_term():
    bm = r.BM25([r.tokenize("wiper motor speed"), r.tokenize("CmdWiperSpeed sent to the wiper"),
                 r.tokenize("mirror")])
    s = bm.scores(r.tokenize("CmdWiperSpeed"))
    assert max(s, key=s.get) == 1 and 2 not in s


# ---------------------------------------------------------------- pipeline over a tiny index (hash embedder)
def _doc(project, doc_id, version, sections):
    return {"doc_id": doc_id, "project_id": project, "doc_type": "hld", "version": version,
            "sections": [{"section_id": sid, "title": title, "page_start": p, "page_end": p, "text": text,
                          "tables": []} for sid, title, p, text in sections]}


FILLER = "The wiper subsystem controls washing and wiping functions of the vehicle. " * 3


@pytest.fixture
def two_releases(dirs):
    for ver, extra in (("R3.1.5", "Old note."), ("R3.2.3", "New note.")):
        index_chunks("p1", chunk_document(_doc("p1", f"bc_r{ver[1:]}", ver, [
            ("4", "Wiper", 8, FILLER + extra),
            ("4.1", "Rain sensing", 9, "Rain sensing overview. The RainSensor reports the rain amount. " + FILLER),
            ("4.1.1", "Evaluator", 10, "The AutoWipingEvaluator receives StaRainAmount and computes "
                                        "ReqFrontAutoWiping for the front wiper."),
        ])), dirs["chroma_dir"])
    index_chunks("p2", chunk_document(_doc("p2", "other", "1.0", [
        ("1", "Brakes", 3, "Secret StaRainAmount brake pressure table for project two only.")])), dirs["chroma_dir"])
    return dirs


def test_hybrid_exact_identifier_and_newest_release(two_releases):
    res = r.retrieve("p1", "Which data element is StaRainAmount?", k=3, cfg=RetrievalConfig(inventory=False),
                     chroma_dir=two_releases["chroma_dir"])
    top = res.hits[0]
    assert "StaRainAmount" in top["text"] and top["metadata"]["doc_id"] == "bc_r3.2.3"  # newest release preferred
    assert res.exact_identifier and "R3.2.3" in res.release_note and "newest" in res.release_note
    assert all(h["metadata"]["project_id"] == "p1" for h in res.hits)


def test_named_and_older_release(two_releases):
    cfg = RetrievalConfig(inventory=False, expand=False)
    res = r.retrieve("p1", "In R3.1.5, what is the wiper note?", k=5, cfg=cfg, chroma_dir=two_releases["chroma_dir"])
    assert res.query.doc_ids == ["bc_r3.1.5"]
    assert {h["metadata"]["doc_id"] for h in res.hits} == {"bc_r3.1.5"}
    old = r.retrieve("p1", "What did the older release say about the wiper?", k=5, cfg=cfg,
                     chroma_dir=two_releases["chroma_dir"])
    assert old.hits[0]["metadata"]["doc_id"] == "bc_r3.1.5"


def test_exact_section_lookup_is_not_semantic_thresholded(two_releases, conn):
    from app import llm, qa
    cfg = RetrievalConfig(inventory=False, expand=False)
    result = qa.ask("p1", "What does section 4.1 describe?", "admin", conn=conn,
                    chroma_dir=two_releases["chroma_dir"], provider=llm.NullProvider(),
                    threshold=1.0, retrieval=cfg, store=False)
    assert result.mode == "generated" and result.built_from == "section_lookup"
    assert "Rain sensing" in result.answer and result.citations
    assert all(c.section_id == "4.1" and c.page_start == 9 for c in result.citations)
    assert all(c.doc_id == "bc_r3.2.3" for c in result.citations)
    assert "project two" not in result.answer


def test_section_lookup_keeps_scope_and_exact_number(two_releases):
    cfg = RetrievalConfig(inventory=False, expand=False)
    for question, doc in [("In R3.1.5, describe section 4.1", "bc_r3.1.5"),
                          ("In R3.2.3, describe section 4.1", "bc_r3.2.3")]:
        result = r.retrieve("p1", question, cfg=cfg, chroma_dir=two_releases["chroma_dir"])
        assert result.section_match
        assert result.hits[0]["metadata"]["doc_id"] == doc
        assert result.hits[0]["metadata"]["section_id"] == "4.1"
    missing = r.retrieve("p1", "Describe section 4.10", cfg=cfg, chroma_dir=two_releases["chroma_dir"])
    assert not missing.section_match
    other = r.retrieve("p2", "Describe section 4.1", cfg=cfg, chroma_dir=two_releases["chroma_dir"])
    assert not other.section_match


def test_section_expansion_adds_parent_within_budget(two_releases):
    cfg = RetrievalConfig(inventory=False, expand=True, context_tokens=10_000)
    res = r.retrieve("p1", "What does the AutoWipingEvaluator compute?", k=1, cfg=cfg,
                     chroma_dir=two_releases["chroma_dir"])
    kinds = [h["metadata"].get("expansion") for h in res.hits]
    assert kinds[0] is None and "parent" in kinds
    parent = next(h for h in res.hits if h["metadata"].get("expansion") == "parent")
    assert parent["metadata"]["section_id"] == "4.1" and parent["metadata"]["page_start"] == 9
    tight = r.retrieve("p1", "What does the AutoWipingEvaluator compute?", k=1,
                       cfg=RetrievalConfig(inventory=False, expand=True, context_tokens=1),
                       chroma_dir=two_releases["chroma_dir"])
    assert len(tight.hits) == 1  # nothing added beyond the budget
    assert len({h["id"] for h in res.hits}) == len(res.hits)  # deduplicated


def test_isolation_hybrid(two_releases):
    for cfg in (RetrievalConfig(), RetrievalConfig.dense_only()):
        res = r.retrieve("p2", "StaRainAmount wiper rain sensing evaluator", k=10, cfg=cfg,
                         chroma_dir=two_releases["chroma_dir"])
        assert res.hits and all(h["metadata"]["project_id"] == "p2" for h in res.hits)
        assert "AutoWipingEvaluator" not in " ".join(h["text"] for h in res.hits)
    assert r.retrieve("nope", "wiper", chroma_dir=two_releases["chroma_dir"]).hits == []


def test_dense_only_matches_search(two_releases):
    q = "rain amount"
    a = r.retrieve("p1", q, k=4, cfg=RetrievalConfig.dense_only(), chroma_dir=two_releases["chroma_dir"]).hits
    b = r.search("p1", q, k=4, chroma_dir=two_releases["chroma_dir"])
    assert [h["id"] for h in a] == [h["id"] for h in b]


def test_bm25_cache_invalidated_on_reindex(two_releases):
    c1 = r.load_corpus("p2", two_releases["chroma_dir"])
    index_chunks("p2", chunk_document(_doc("p2", "other2", "1.0", [("1", "X", 1, "Fresh text.")])),
                 two_releases["chroma_dir"])
    c2 = r.load_corpus("p2", two_releases["chroma_dir"])
    assert c2 is not c1 and len(c2.ids) == len(c1.ids) + 1


# ---------------------------------------------------------------- inventory evidence
def _inv_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    extract.ensure_schema(c)
    return c


def _add(c, table, name, page, doc="doc_r1", version="R1", section="4.1 Wiper", project="p", **fields):
    c.execute(f"INSERT INTO {table}(project_id, doc_id, version, name, norm_name, fields, page, section, "
              "sources_json, extraction_method, confidence, review_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (project, doc, version, name, extract.normalize_name(name), json.dumps(fields), page, section, "[]",
               "diagram", "Medium", "unreviewed"))


def test_inventory_evidence_instance_type_names_and_newest_release():
    c = _inv_db()
    for doc, ver in (("doc_r1", "R1"), ("doc_r2", "R2")):
        _add(c, "ports", "StaRainAmount", 10, doc=doc, version=ver, section="4.1.1 Rain",
             component="AutoWimpingEvaluator::AutoWipingEvaluator", direction="in")
    hits = qa.inventory_hits("p", "Which ports does the AutoWipingEvaluator have?", c)
    assert len(hits) == 1 and hits[0]["metadata"]["doc_id"] == "doc_r2"  # newest release only
    assert "StaRainAmount" in hits[0]["text"] and "direction in" in hits[0]["text"]
    assert hits[0]["metadata"]["page_start"] == 10 and hits[0]["metadata"]["inventory"] is True
    assert qa.ocr_note(hits[0]["metadata"]) == "from extracted inventory"


def test_inventory_gap_shows_subsection_ports_never_absence():
    c = _inv_db()
    _add(c, "components", "WiperWasher", 9, section="4.1 Wiper/Washer")
    _add(c, "ports", "ReqFrontAutoWiping", 10, section="4.1.1 Rain sensing", component="AutoWipingEvaluator")
    hits = qa.inventory_hits("p", "Which ports does the WiperWasher component have?", c)
    text = hits[0]["text"]
    assert "possible extraction gap" in text and "does not mean" in text
    assert "ReqFrontAutoWiping of AutoWipingEvaluator" in text
    assert hits[0]["metadata"]["page_start"] == 9 and hits[0]["metadata"]["page_end"] == 10


def test_inventory_signal_identifier():
    c = _inv_db()
    _add(c, "signals", "ReqCarFinder", 14, sender="Remote Key", receivers=["Interior Light Manager"])
    hits = qa.inventory_hits("p", "Who sends ReqCarFinder?", c)
    assert "sender Remote Key" in hits[0]["text"] and hits[0]["metadata"]["page_start"] == 14


def test_inventory_evidence_isolated_per_project():
    c = _inv_db()
    _add(c, "ports", "SecretPort", 3, project="other", component="WiperWasher")
    _add(c, "components", "WiperWasher", 9)
    hits = qa.inventory_hits("p", "Which ports does the WiperWasher have?", c)
    assert "SecretPort" not in hits[0]["text"] and "extraction gap" in hits[0]["text"]


# ---------------------------------------------------------------- QA integration
def test_ask_reports_release_used(conn, two_releases):
    a = qa.ask("p1", "What does the AutoWipingEvaluator compute?", "view", conn=conn,
               chroma_dir=two_releases["chroma_dir"], threshold=-1.0)
    assert a.mode == "evidence_only"
    assert "Answered from release R3.2.3" in a.limitation
    assert all(c.page_start for c in a.citations)


def test_only_not_stated_becomes_refusal(conn, two_releases):
    class P:
        def generate(self, prompt, system="", schema=None):
            return f"{qa.NOT_STATED} [C1]."
    a = qa.ask("p1", "What does the AutoWipingEvaluator compute?", "view", conn=conn,
               chroma_dir=two_releases["chroma_dir"], provider=P(), threshold=-1.0)
    assert a.mode == "refused" and a.confidence == "Low" and not a.citations


def test_extraction_gap_always_stated_in_limitation(conn, two_releases):
    extract.ensure_schema(conn)
    _add(conn, "components", "AutoWipingEvaluator", 10, doc="bc_r3.2.3", version="R3.2.3", project="p1")

    class P:
        def generate(self, prompt, system="", schema=None):
            return "The AutoWipingEvaluator has no listed ports [C1]."
    a = qa.ask("p1", "Which ports does the AutoWipingEvaluator have?", "view", conn=conn,
               chroma_dir=two_releases["chroma_dir"], provider=P(), threshold=-1.0)
    # the model's "has no listed ports" claim is replaced by the plain extraction-gap statement, with citations
    assert (a.mode, a.refusal_reason) == ("refused", "not_extracted")
    assert a.answer.startswith("No ports were extracted for AutoWipingEvaluator in bc_r3.2.3; this may be a gap")
    assert "has no listed ports" not in a.answer and a.citations[0].note == "from extracted inventory"
