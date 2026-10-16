"""Non-LLM unit tests: eval/run_eval.py metric functions on tiny fixtures."""
from __future__ import annotations

import csv

import pytest

from eval import run_eval as ev


def test_prf():
    r = ev.prf({"a", "b", "c", "d"}, {"a", "b", "x"})
    assert r["tp"] == 2 and r["fp"] == 1 and r["fn"] == 2
    assert r["precision"] == pytest.approx(2 / 3) and r["recall"] == 0.5
    assert r["f1"] == pytest.approx(2 * (2 / 3) * 0.5 / (2 / 3 + 0.5))
    assert ev.prf(set(), set())["f1"] == 0.0 or ev.prf(set(), set())["recall"] == 1.0
    assert ev.prf({"a"}, set())["precision"] == 0.0


def test_percentile():
    assert ev.percentile([], 50) is None
    assert ev.percentile([5], 95) == 5
    assert ev.percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert ev.percentile([10, 20, 30, 40, 50], 95) == pytest.approx(48.0)


def test_key_facts():
    facts = [["50 ms", "50ms"], ["window"]]
    assert ev.key_fact_hits("A Window request takes 50ms.", facts) == (2, 2)
    assert ev.key_fact_hits("Only the window.", facts) == (1, 2)


def test_citation_and_section():
    cites = [{"doc_id": "d1", "page_start": 4, "page_end": 5, "section_id": "2.1"}]
    assert ev.citation_correct(cites, ["d1"], [5])
    assert not ev.citation_correct(cites, ["d1"], [6])
    assert not ev.citation_correct(cites, ["d2"], [4])
    assert ev.section_correct(cites, ["d1"], "2")
    assert not ev.section_correct(cites, ["d1"], "3")
    assert not ev.section_correct(cites, ["d1"], "")


def test_groundedness_and_refusal():
    g = ev.sentence_groundedness("The motor moves [C1]. It is fast. Not stated in the provided documents for torque.")
    assert g["claims"] == 2 and g["cited"] == 1 and g["share"] == 0.5
    assert ev.sentence_groundedness("")["share"] is None
    assert ev.is_refusal("I cannot answer this from the project documents.", "refused")
    assert ev.is_refusal("Not stated in the provided documents", "generated")
    assert not ev.is_refusal("It is 50 ms [C1].", "generated")


def test_calibration():
    rows = [{"confidence": "High", "correct": True}, {"confidence": "High", "correct": False},
            {"confidence": "Low", "correct": True}]
    c = ev.calibration(rows)
    assert c["High"] == {"n": 2, "correct": 1, "accuracy": 0.5}
    assert c["Medium"]["accuracy"] is None and c["Low"]["accuracy"] == 1.0


def test_name_match():
    assert ev.name_match("ButtonPanel", "Button Panel Component")
    assert ev.name_match("AutoWipingEvaluator", "AutoWimpingEvaluator::AutoWipingEvaluator")
    assert not ev.name_match("FrontWiper", "FrontWasher")


def test_score_real_gold():
    gold = [{"doc_id": "d", "name": "FrontWiper", "type": "component", "component": "", "direction": "", "page": "9"},
            {"doc_id": "d", "name": "NozzleHeater", "type": "component", "component": "", "direction": "", "page": "9"},
            {"doc_id": "d", "name": "StaRainAmount", "type": "port", "component": "RainSensor", "direction": "provided", "page": "10"}]
    inv = {"d": {"components": [{"name": "FrontWiper", "page": 9}, {"name": "Junk", "page": 9}],
                 "ports": [], "interfaces": [], "signals": [{"name": "StaRainAmount", "page": 10}]}}
    s = ev.score_real_gold(gold, inv)
    assert s["component"]["recall_typed"] == 0.5 and s["component"]["precision_page_scoped"] == 0.5
    assert s["port"]["recall_typed"] == 0.0 and s["port"]["recall_any_type"] == 1.0


def test_load_gold_verified_only(tmp_path):
    p = tmp_path / "g.csv"
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["doc_id", "name", "type", "verified"])
        w.writerow(["d", "A", "component", "yes"])
        w.writerow(["d", "B", "component", "no"])
    assert [r["name"] for r in ev.load_gold_csv(p, True)] == ["A"]
    assert len(ev.load_gold_csv(p, False)) == 2


def test_latency_stats():
    s = ev.lat_stats([100.0, 200.0, 300.0])
    assert s["n"] == 3 and s["p50_ms"] == 200.0 and s["mean_ms"] == 200.0
    assert ev.lat_stats([])["p50_ms"] is None


def test_gold_files_valid():
    import json
    from pathlib import Path
    q = json.loads((Path(ev.EVAL_DIR) / "gold_questions.json").read_text(encoding="utf-8"))["questions"]
    cats = [x["category"] for x in q]
    assert len(q) == 25 and cats.count("real") == 12 and cats.count("synthetic") == 8 and cats.count("unanswerable") == 5
    assert all(x["key_facts"] for x in q if x["category"] != "unanswerable")
    assert len({x["id"] for x in q}) == 25


def test_heldout_generator_and_truth(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("gen3", Path(ev.ROOT) / "data/synthetic/generate_heldout.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    paths = mod.generate(tmp_path)
    assert paths["pdf"].stat().st_size > 2000
    gt = mod.ground_truth()
    assert len(gt["planted_defects"]) == 6
    assert "prose" in set(gt["port_described_in"].values()) and "table" in set(gt["port_described_in"].values())


def _run(correct, refusal, p95, f1):
    return {"C_qa": {"summary": {"answer_correctness": correct, "refusal_accuracy_unanswerable": refusal,
                                 "latency_wall_ms": {"p95_ms": p95}}}, "A_mean_f1": f1}


def _R(cand_correct, cand_refusal=1.0, p95=5000, share=1.0, cand_f1=0.70):
    return {"models": {ev.BASELINE: {"run1": _run(0.75, 1.0, 4000, 0.70), "run2": _run(0.75, 1.0, 4000, 0.70),
                                     "placement": {"gpu_share": 1.0}},
                       ev.CANDIDATE: {"run1": _run(cand_correct, cand_refusal, p95, cand_f1),
                                      "run2": _run(cand_correct, cand_refusal, p95, cand_f1),
                                      "placement": {"gpu_share": share}}}}


def test_decision_rule_switches_only_when_all_conditions_hold():
    assert ev.decide(_R(0.85))["switch"] is True
    assert ev.decide(_R(0.79))["switch"] is False                      # +4 points: no real difference
    assert ev.decide(_R(0.75, cand_f1=0.76))["switch"] is True        # +0.06 real-gold F1 is enough
    assert ev.decide(_R(0.85, cand_refusal=0.8))["failed"] == ["refusal_not_lower"]
    assert ev.decide(_R(0.85, p95=25000))["failed"] == ["p95_under_20s"]
    d = ev.decide(_R(0.85, share=0.67))
    assert d["failed"] == ["fits_gpu"] and d["default"] == ev.BASELINE
    assert "no real difference" in ev.decide(_R(0.79))["reason"]


def test_v4_lock_matches_frozen_files():
    out = ev.verify_v4_lock()
    assert out and all(v["ok"] for v in out.values())


def test_latency_from_trace_slice_ignores_cached_and_other_models():
    ents = [{"model": "m", "purpose": "qa", "latency_ms": 100, "cached": False},
            {"model": "m", "purpose": "qa", "latency_ms": 1, "cached": True},
            {"model": "x", "purpose": "qa", "latency_ms": 999, "cached": False},
            {"model": "m", "purpose": "extract_prose", "latency_ms": 50, "chunk_ids": ["d:4.1:0"]},
            {"model": "m", "purpose": "extract_prose", "latency_ms": 70, "chunk_ids": ["d:4.1:1"]}]
    lat = ev.latency_from_traces("m", ents)
    assert lat["qa_generation"]["n"] == 1 and lat["extract_per_section"]["p50_ms"] == 120
