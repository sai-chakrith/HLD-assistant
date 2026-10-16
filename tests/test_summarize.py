"""Component and flow reports on the synthetic HLD."""
import json
import re

import pytest

from app import summarize


def test_component_report_cited(syn_env):
    conn = syn_env["conn"]
    rep = summarize.component_report("p1", syn_env["doc_id"], "1.0", "WinCtrlManager", conn=conn)
    assert rep["purpose"] == "LLM not configured" and rep["confidence"] == "Low"
    assert rep["review_status"] == "unreviewed" and rep["id"]
    assert rep["ports"] and all(p["citation"]["page"] for p in rep["ports"])
    assert rep["upstream"] or rep["downstream"]
    for key in ("ports", "interfaces", "upstream", "downstream", "signals_sent", "signals_received"):
        assert all(i["citation"]["doc_id"] == syn_env["doc_id"] for i in rep[key])
    assert rep["source_pages"] and rep["citation"]["page"]
    stored = conn.execute("SELECT content_json, review_status FROM reports WHERE id=?", (rep["id"],)).fetchone()
    assert json.loads(stored[0])["component"] == "WinCtrlManager" and stored[1] == "unreviewed"


def test_component_report_unknown(syn_env):
    with pytest.raises(ValueError):
        summarize.component_report("p1", syn_env["doc_id"], "1.0", "Nope", conn=syn_env["conn"])


def test_flow_summary_steps_and_unknown(syn_env):
    conn = syn_env["conn"]
    rep = summarize.functional_flow_summary("p1", syn_env["doc_id"], "1.0", conn=conn,
                                            processed_dir=syn_env["processed_dir"])
    assert len(rep["flows"]) == 3
    steps = rep["flows"][0]["steps"]
    assert [s["n"] for s in steps] == [1, 2, 3, 4, 5] and all(s["citation"]["page"] for s in steps)
    assert "WinSwitchHandler" in rep["components_referenced"] and rep["flagged_steps"] == 0
    # remove a component from the inventory -> its mentions become flagged
    conn.execute("DELETE FROM components WHERE name='MotorDriver'")
    conn.commit()
    rep2 = summarize.functional_flow_summary("p1", syn_env["doc_id"], "1.0", conn=conn,
                                             processed_dir=syn_env["processed_dir"])
    assert rep2["flagged_steps"] >= 1
    assert any("MotorDriver" in s["unknown_entities"] for f in rep2["flows"] for s in f["steps"])


def test_document_summary_degrades(syn_env):
    rep = summarize.document_summary("p1", syn_env["doc_id"], "1.0", conn=syn_env["conn"],
                                     processed_dir=syn_env["processed_dir"])
    assert rep["summary"] == "LLM not configured" and rep["confidence"] == "Low"


# ---------------------------------------------------------------- Phase D: LLM-backed reports (mocked provider)
class Fake:
    name = "fake"
    model = "fake-model"

    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def generate(self, prompt, system="", schema=None, model=None, purpose="", chunk_ids=None):
        self.calls.append((purpose, prompt))
        return self.fn(prompt, purpose)


def test_cited_filter_drops_uncited_and_invented():
    kept, labels, dropped = summarize.cited_filter(
        "A does x [C1]. B does y. C does z [C9]. D does w [F1, C7].", {"C1", "F1", "C2"})
    assert kept == ["A does x [C1].", "D does w [F1]."] and labels == ["C1", "F1"] and dropped == 2


def test_component_purpose_uncited_and_invented_dropped(syn_env):
    prov = Fake(lambda p, purpose: "It drives the window [F1]. Totally invented claim. Another one [C99]. "
                                   "It has ports [F3, C1].")
    rep = summarize.component_report("p1", syn_env["doc_id"], "1.0", "WinCtrlManager", conn=syn_env["conn"],
                                     provider=prov, chroma_dir=syn_env["chroma_dir"])
    assert rep["purpose"] == "It drives the window [F1]. It has ports [F3, C1]."
    assert {x["label"] for x in rep["purpose_citations"]} == {"F1", "F3", "C1"}
    assert all(x["doc_id"] == syn_env["doc_id"] for x in rep["purpose_citations"])
    assert rep["model"] == "fake-model" and "uncited" in rep["limitation"]
    assert rep["review_status"] == "unreviewed" and rep["confidence"] in ("Medium", "High")
    assert "[F1]" in prov.calls[0][1] and "[C1]" in prov.calls[0][1]


def test_component_purpose_nothing_valid_withheld(syn_env):
    prov = Fake(lambda p, purpose: "No labels here. Or here [C42].")
    rep = summarize.component_report("p1", syn_env["doc_id"], "1.0", "WinCtrlManager", conn=syn_env["conn"],
                                     provider=prov, chroma_dir=syn_env["chroma_dir"])
    assert rep["confidence"] == "Low" and rep["purpose_citations"] == [] and "withheld" in rep["limitation"]
    assert rep["ports"]  # structured sections still present


def test_component_purpose_llm_unreachable_degrades(syn_env):
    def boom(p, purpose):
        raise summarize.llm.LLMNotConfigured("down")
    rep = summarize.component_report("p1", syn_env["doc_id"], "1.0", "WinCtrlManager", conn=syn_env["conn"],
                                     provider=Fake(boom), chroma_dir=syn_env["chroma_dir"])
    assert rep["purpose"] == "LLM not configured" and rep["confidence"] == "Low"


def test_document_summary_map_reduce_keeps_citations(syn_env):
    def fn(prompt, purpose):
        lab = re.search(r"PASSAGE \[(S\d+)\]", prompt)
        if purpose == "doc_summary_map":
            return f"- Section does a thing [{lab.group(1)}].\n- Unlabelled bullet.\n- Fake [S99]."
        return "The document describes things [S1, S2]. Invented overview sentence. Bogus [S77]."
    prov = Fake(fn)
    rep = summarize.document_summary("p1", syn_env["doc_id"], "1.0", conn=syn_env["conn"],
                                     processed_dir=syn_env["processed_dir"], provider=prov, min_chars=20)
    assert rep["summary"] == "The document describes things [S1, S2]."
    assert [x["label"] for x in rep["citations"]] == ["S1", "S2"]
    assert all(x["page"] and x["section"] for x in rep["citations"])
    assert rep["bullets"] and all(b["text"].endswith(f"[{b['label']}].") for b in rep["bullets"])
    assert not any("Unlabelled" in b["text"] or "S99" in b["text"] for b in rep["bullets"])
    assert rep["model"] == "fake-model" and rep["confidence"] in ("Low", "Medium", "High")
    assert sum(1 for p, _ in prov.calls if p == "doc_summary_reduce") == 1
    assert "front-matter" in rep["limitation"]


def test_document_summary_unreachable_degrades(syn_env):
    def boom(p, purpose):
        raise summarize.llm.LLMNotConfigured("down")
    rep = summarize.document_summary("p1", syn_env["doc_id"], "1.0", conn=syn_env["conn"],
                                     processed_dir=syn_env["processed_dir"], provider=Fake(boom))
    assert rep["summary"] == "LLM not configured" and rep["confidence"] == "Low"


def test_flow_summary_derived_from_graph_when_no_flow_sections(syn_env, monkeypatch):
    monkeypatch.setattr(summarize, "flow_sections", lambda doc: [])
    rep = summarize.functional_flow_summary("p1", syn_env["doc_id"], "1.0", conn=syn_env["conn"],
                                            processed_dir=syn_env["processed_dir"])
    assert rep["source"] == "graph" and rep["flows"] and "derived" in rep["limitation"]
    steps = [s for f in rep["flows"] for s in f["steps"]]
    assert steps and all(s["citation"]["page"] for s in steps) and rep["flagged_steps"] == 0
    syn_env["conn"].execute("DELETE FROM components WHERE name='MotorDriver'")
    syn_env["conn"].commit()
    rep2 = summarize.functional_flow_summary("p1", syn_env["doc_id"], "1.0", conn=syn_env["conn"],
                                             processed_dir=syn_env["processed_dir"])
    assert rep2["flagged_steps"] >= 1


@pytest.mark.llm
def test_real_component_report(syn_env):
    rep = summarize.component_report("p1", syn_env["doc_id"], "1.0", "WinCtrlManager", conn=syn_env["conn"],
                                     provider=summarize.llm.OllamaProvider(db_path=syn_env["conn"].execute(
                                         "PRAGMA database_list").fetchone()[2], trace_path=syn_env["processed_dir"] / "t.jsonl"),
                                     chroma_dir=syn_env["chroma_dir"])
    assert rep["purpose"] != "LLM not configured"
    assert re.search(r"\[[CF]\d+", rep["purpose"]) or rep["confidence"] == "Low"


@pytest.mark.llm
def test_real_document_summary(syn_env):
    rep = summarize.document_summary("p1", syn_env["doc_id"], "1.0", conn=syn_env["conn"],
                                     processed_dir=syn_env["processed_dir"], max_pieces_per_section=1,
                                     provider=summarize.llm.OllamaProvider(db_path=syn_env["conn"].execute(
                                         "PRAGMA database_list").fetchone()[2], trace_path=syn_env["processed_dir"] / "t.jsonl"))
    assert rep["summary"] != "LLM not configured" and rep["bullets"]
    assert all(c["page"] for c in rep["citations"])


def test_cited_filter_accepts_parenthesised_labels():
    kept, labels, dropped = summarize.cited_filter("It does x (F1). It does y (C2, F1).", {"F1", "C2"})
    assert kept == ["It does x [F1].", "It does y [C2, F1]."] and dropped == 0


def test_cited_filter_support_check_repairs_or_drops():
    texts = {"F1": "Box drawn in diagram", "C1": "The light group controls interior lamps and receives driver wishes"}
    kept, labels, dropped = summarize.cited_filter(
        "It receives driver wishes and controls interior lamps [F1]. Quantum entanglement powers flux capacitors [F1].",
        {"F1", "C1"}, texts)
    assert kept == ["It receives driver wishes and controls interior lamps [C1]."] and labels == ["C1"] and dropped == 1
