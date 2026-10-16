"""Dev-set evaluation for retrieval tuning (eval/dev_questions.json; NOT the gold set).

Retrieval is scored without the LLM (hit@1 / hit@5 of an expected doc+page, refusal decision at the threshold),
end-to-end correctness separately with the local LLM (--e2e). Ablation ladder: dense only, +BM25, +query
understanding/version policy, +inventory evidence, +section expansion, +reranker.

    .venv\\Scripts\\python -m eval.dev_eval                 # retrieval-only ablation (fast)
    .venv\\Scripts\\python -m eval.dev_eval --e2e           # + end-to-end with qwen2.5:3b (LLM calls are cached)
    .venv\\Scripts\\python -m eval.dev_eval --sweep         # threshold sweep for the full configuration
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("EMBEDDER", None)

from app import controls, qa, retrieve  # noqa: E402
from app.config import settings  # noqa: E402
from eval.run_eval import calibration, is_refusal, key_fact_hits, percentile  # noqa: E402

EVAL_DIR = ROOT / "eval"
WORK = EVAL_DIR / "_work"
RC = retrieve.RetrievalConfig
FULL = RC(hybrid=True, query_understanding=True, inventory=True, expand=True, rerank=False)
LADDER = [
    ("dense only", RC.dense_only()),
    ("+BM25 (RRF)", RC(hybrid=True, query_understanding=False, inventory=False, expand=False)),
    ("+query understanding / version policy", RC(hybrid=True, query_understanding=True, inventory=False,
                                                  expand=False)),
    ("+inventory evidence", RC(hybrid=True, query_understanding=True, inventory=True, expand=False)),
    ("+section expansion", FULL),
    ("+reranker", replace(FULL, rerank=True)),
]


def load_questions() -> list[dict]:
    return json.loads((EVAL_DIR / "dev_questions.json").read_text(encoding="utf-8"))["questions"]


def hit_ok(h: dict, q: dict) -> bool:
    m = h["metadata"]
    if m.get("doc_id") not in q["expected_docs"]:
        return False
    lo, hi = m.get("page_start"), m.get("page_end") or m.get("page_start")
    return lo is not None and any(lo <= p <= hi for p in q["expected_pages"])


def dev_db() -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    dst = WORK / "dev.db"
    for suf in ("", "-wal", "-shm"):
        Path(str(dst) + suf).unlink(missing_ok=True)
    shutil.copy(settings.db_path, dst)
    return dst


def offtopic(r) -> bool:
    """qa's retrieval-stage off-topic rule (term coverage, no named component/identifier)."""
    named = r.on_topic or r.exact_identifier or bool(r.query.components)
    return os.environ.get("QA_OFFTOPIC", "1") != "0" and not named and r.coverage < qa.OFFTOPIC_COVERAGE


def retrieval_rows(conn, cfg, questions, k: int, thr: float) -> list[dict]:
    rows = []
    for q in questions:
        t0 = time.perf_counter()
        r = retrieve.retrieve(q["project_id"], q["question"], k=k, cfg=cfg, conn=conn)
        ms = (time.perf_counter() - t0) * 1000
        primary = [h for h in r.hits if not h["metadata"].get("expansion")]
        rows.append({"id": q["id"], "category": q["category"], "ms": ms, "top": r.top_score, "on_topic": r.on_topic,
                     "exact": r.exact_identifier,
                     "coverage": r.coverage,
                     "refuse": (not r.hits) or offtopic(r) or not (
                         r.top_score >= thr or r.on_topic or
                         (r.exact_identifier and r.top_score >= thr - qa.IDENTIFIER_MARGIN)),
                     "hit1": bool(primary) and hit_ok(primary[0], q),
                     "hit5": any(hit_ok(h, q) for h in primary[:5]),
                     "any": any(hit_ok(h, q) for h in r.hits),
                     "top_doc": primary[0]["metadata"].get("doc_id") if primary else None,
                     "release_note": r.release_note, "qtype": r.query.qtype, "refuse_offtopic": offtopic(r)})
    return rows


def summarize(rows: list[dict]) -> dict:
    ans = [r for r in rows if r["category"] != "unanswerable"]
    un = [r for r in rows if r["category"] == "unanswerable"]
    sh = lambda rs, key: sum(1 for r in rs if r[key]) / len(rs) if rs else None  # noqa: E731
    ms = [r["ms"] for r in rows]
    out = {"hit@1": sh(ans, "hit1"), "hit@5": sh(ans, "hit5"), "hit_any_incl_expansion": sh(ans, "any"),
           "refusal_accuracy": sh(un, "refuse"), "false_refusals": sum(1 for r in ans if r["refuse"]),
           "retrieval_ms_p50": percentile(ms, 50), "retrieval_ms_p95": percentile(ms, 95)}
    for cat in ("ports", "version", "identifier", "general"):
        rs = [r for r in ans if r["category"] == cat]
        out[f"hit@5_{cat}"] = sh(rs, "hit5")
    return out


def e2e_rows(conn, cfg, questions, k: int, model: str) -> list[dict]:
    rows = []
    for q in questions:
        t0 = time.perf_counter()
        a = qa.ask(q["project_id"], q["question"], "admin", k=k, conn=conn, model=model, retrieval=cfg)
        ms = (time.perf_counter() - t0) * 1000
        refused = is_refusal(a.answer, a.mode)
        row = {"id": q["id"], "category": q["category"], "ms": ms, "mode": a.mode, "refused": refused,
               "confidence": a.confidence,
               "answer": a.answer, "limitation": a.limitation,
               "cited": [(c.doc_id, c.page_start, c.page_end) for c in a.citations]}
        if q["category"] == "unanswerable":
            row["correct"] = refused
        else:
            hit, tot = key_fact_hits(a.answer, q["key_facts"])
            row.update({"facts": hit, "facts_total": tot, "correct": (not refused) and hit == tot,
                        "cite_ok": any(hit_ok({"metadata": {"doc_id": c.doc_id, "page_start": c.page_start,
                                                            "page_end": c.page_end}}, q) for c in a.citations)})
        rows.append(row)
        print(f"   {q['id']} {a.mode:9} correct={row['correct']} {ms / 1000:.1f}s", flush=True)
    return rows


def summarize_e2e(rows: list[dict]) -> dict:
    ans = [r for r in rows if r["category"] != "unanswerable"]
    un = [r for r in rows if r["category"] == "unanswerable"]
    ms = [r["ms"] for r in rows]
    return {"e2e_correct": sum(r["correct"] for r in ans) / len(ans),
            "key_fact_recall": sum(r["facts"] for r in ans) / sum(r["facts_total"] for r in ans),
            "citation_ok": sum(r["cite_ok"] for r in ans) / len(ans),
            "e2e_refusal_accuracy": sum(r["correct"] for r in un) / len(un) if un else None,
            "e2e_false_refusals": sum(r["refused"] for r in ans),
            "e2e_ms_p50": percentile(ms, 50), "e2e_ms_p95": percentile(ms, 95),
            "calibration": calibration([{"confidence": r.get("confidence"), "correct": r["correct"]} for r in rows])}


def sweep(conn, cfg, questions, k: int) -> list[dict]:
    rows = retrieval_rows(conn, cfg, questions, k, 0.0)
    out = []
    for t in [x / 100 for x in range(55, 76)]:
        ans = [r for r in rows if r["category"] != "unanswerable"]
        un = [r for r in rows if r["category"] == "unanswerable"]
        ref = lambda r: r["refuse_offtopic"] or not (r["top"] >= t or r["on_topic"]  # noqa: E731
                                                     or (r["exact"] and r["top"] >= t - qa.IDENTIFIER_MARGIN))
        out.append({"threshold": t, "refusal_accuracy": sum(ref(r) for r in un) / len(un),
                    "false_refusals": sum(ref(r) for r in ans)})
    print("top dense score per question:")
    for r in sorted(rows, key=lambda r: r["top"]):
        print(f"   {r['id']} {r['category']:12} top={r['top']:.3f} on_topic={r['on_topic']}")
    return out


def pct(x):
    return "n/a" if x is None else f"{x * 100:.0f}%"


def render(R: dict) -> str:
    L = ["# Dev-set retrieval ablation", "",
         f"Generated {R['generated']}. Dev set: eval/dev_questions.json ({R['n']} questions: 8 ports, 6 version, "
         "5 identifier, 5 unanswerable, 6 general), written from the PDFs/inventory. The gold set "
         "(eval/gold_questions.json, eval/real_gold.csv) was NOT used for any tuning. "
         f"k={R['k']}, threshold {R['threshold']:.2f}, model {R['model']} (LLM calls cached; latency is end-to-end "
         "wall time, cached generations included). hit@k: an expected doc+page among the first k retrieved chunks "
         "(inventory chunks count, section-expansion chunks do not). Refusal accuracy: unanswerable questions "
         "refused (retrieval columns: refused at the threshold before the LLM; e2e: final answer refused).", "",
         "| configuration | hit@1 | hit@5 | hit@5 ports | hit@5 version | hit@5 identifier | refusal acc. (retrieval) "
         "| false refusals | e2e correct | key-fact recall | e2e refusal acc. | retrieval p50/p95 ms | e2e p50/p95 s |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, s in R["ablation"].items():
        e2 = s.get("e2e") or {}
        L.append(f"| {name} | {pct(s['hit@1'])} | {pct(s['hit@5'])} | {pct(s['hit@5_ports'])} | "
                 f"{pct(s['hit@5_version'])} | {pct(s['hit@5_identifier'])} | {pct(s['refusal_accuracy'])} | "
                 f"{s['false_refusals']} | {pct(e2.get('e2e_correct'))} | {pct(e2.get('key_fact_recall'))} | "
                 f"{pct(e2.get('e2e_refusal_accuracy'))} | {s['retrieval_ms_p50']:.0f}/{s['retrieval_ms_p95']:.0f} | "
                 + (f"{e2['e2e_ms_p50'] / 1000:.1f}/{e2['e2e_ms_p95'] / 1000:.1f}" if e2 else "n/a") + " |")
    if R.get("sweep"):
        L += ["", "## Threshold sweep (full configuration, retrieval-level refusal)", "",
              "| threshold | refusal accuracy | false refusals (of 25) |", "|---|---|---|"]
        L += [f"| {x['threshold']:.2f} | {pct(x['refusal_accuracy'])} | {x['false_refusals']} |" for x in R["sweep"]]
    if R.get("changes"):
        L += ["", "## Part B changes (dev set, end-to-end, default retrieval, qwen2.5:3b)", "",
              "Each row is the full dev set after that change (cumulative, in order). Calibration = share correct "
              "among answers with that confidence (n in brackets).", "",
              "| change | e2e correct | key-fact recall | refusal acc. | false refusals | refused at retrieval "
              "(unanswerable / answerable) | High | Medium | Low | flipped vs previous row |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        prev = None
        for name, ch in R["changes"].items():
            e, cal = ch["e2e"], ch["e2e"].get("calibration", {})
            cell = lambda lvl: (f"{pct(cal[lvl]['accuracy'])} ({cal[lvl]['n']})" if cal.get(lvl, {}).get("n")  # noqa: E731
                                else "-")
            flips = []
            if prev:
                flips = [f"{i} {'+' if ok else '-'}" for i, ok in ch["correct"].items() if prev.get(i) != ok]
            L.append(f"| {name} | {pct(e['e2e_correct'])} | {pct(e['key_fact_recall'])} | "
                     f"{pct(e['e2e_refusal_accuracy'])} | {e['e2e_false_refusals']} | "
                     + (f"{pct(ch['retrieval_refusal'])} / {ch['retrieval_false_refusals']}"
                        if "retrieval_refusal" in ch else "n/a") + f" | {cell('High')} | "
                     f"{cell('Medium')} | {cell('Low')} | {', '.join(flips) or '-'} |")
            prev = ch["correct"]
    for title, lines in (R.get("sections") or {}).items():
        L += ["", f"## {title}", ""] + lines
    if R.get("notes"):
        L += ["", "## Notes", ""] + [f"- {n}" for n in R["notes"]]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--e2e", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--model", default=settings.ollama_model)
    ap.add_argument("--only", nargs="*", help="configuration names (prefix match) to run")
    ap.add_argument("--note", action="append", default=[])
    ap.add_argument("--change", help="label: run the default configuration end-to-end and record it as a Part B row")
    a = ap.parse_args()
    questions = load_questions()
    conn = controls.get_conn(dev_db())
    thr = settings.similarity_threshold
    out_json = EVAL_DIR / "dev_results.json"
    R = json.loads(out_json.read_text(encoding="utf-8")) if out_json.exists() else {"ablation": {}, "rows": {}}
    R.update({"generated": time.strftime("%Y-%m-%d %H:%M"), "n": len(questions), "k": a.k, "threshold": thr,
              "model": a.model})
    if a.change:
        print(f"== change: {a.change}", flush=True)
        rr = summarize(retrieval_rows(conn, RC.from_settings(), questions, a.k, thr))
        er = e2e_rows(conn, RC.from_settings(), questions, a.k, a.model)
        R.setdefault("changes", {})[a.change] = {"e2e": summarize_e2e(er), "rows": er,
                                                 "retrieval_refusal": rr["refusal_accuracy"],
                                                 "retrieval_false_refusals": rr["false_refusals"],
                                                 "correct": {r["id"]: r["correct"] for r in er}}
        print(json.dumps(R["changes"][a.change]["e2e"], indent=1, default=str), flush=True)
        out_json.write_text(json.dumps(R, indent=2, default=str), encoding="utf-8")
        (EVAL_DIR / "dev_results.md").write_text(render(R), encoding="utf-8")
        conn.close()
        return 0
    for name, cfg in LADDER:
        if a.only and not any(name.startswith(o) for o in a.only):
            continue
        print(f"== {name}", flush=True)
        rows = retrieval_rows(conn, cfg, questions, a.k, thr)
        s = summarize(rows)
        s["config"] = asdict(cfg)
        if a.e2e:
            er = e2e_rows(conn, cfg, questions, a.k, a.model)
            s["e2e"] = summarize_e2e(er)
            R["rows"][name + " (e2e)"] = er
        R["ablation"][name] = s
        R["rows"][name] = rows
        print(json.dumps({x: y for x, y in s.items() if x != "config"}, indent=1, default=str), flush=True)
    if a.sweep:
        R["sweep"] = sweep(conn, FULL, questions, a.k)
    R["notes"] = a.note or R.get("notes", [])
    order = [n for n, _ in LADDER]
    R["ablation"] = {n: R["ablation"][n] for n in order if n in R["ablation"]}
    out_json.write_text(json.dumps(R, indent=2, default=str), encoding="utf-8")
    (EVAL_DIR / "dev_results.md").write_text(render(R), encoding="utf-8")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
