"""Evaluation harness: one clean comparison of two local models (qwen2.5:3b vs qwen3:4b-instruct) in six blocks.

  A. real-document extraction vs eval/real_gold.csv (51 verified rows), per model + deterministic baseline
  B. held-out v4 extraction P/R/F1 + planted defects (sha256 verified against eval/heldout_v4.lock first)
  C. gold Q&A (25 questions): correctness, key facts, citations, groundedness, refusals, calibration
  D. cited summaries: 5 component reports, 2 document summaries
  E. controls (run once): unapproved upload, cross-project isolation, viewer cannot approve, offline run
  F. speed and memory: Q&A / extraction latency (warm run), cold start, peak VRAM, `ollama ps` placement

Usage:
  python -m eval.run_eval                       # both models, two runs each (cache reads off), ~1-2 h
  python -m eval.run_eval --render-only         # re-render results.md and charts from results.json

Each model run uses its own COPY of data/hld.db under eval/_work/. The protocol and the decision rule are written to
eval/results.md before any model runs. Nothing here adapts the parser or the prompts.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # embedder is cached locally; avoids network probes
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


EVAL_DIR = ROOT / "eval"


WORK = EVAL_DIR / "_work"


CHARTS = EVAL_DIR / "charts"


TYPES = ("components", "ports", "interfaces", "signals")


GOLD_TYPE = {"component": "components", "port": "ports", "interface": "interfaces", "signal": "signals"}


def prf(gt: set, ex: set) -> dict:
    """Precision / recall / F1 of two key sets. Empty extraction -> P=1 by convention only if gt is also empty."""
    tp = len(gt & ex)
    p = tp / len(ex) if ex else (1.0 if not gt else 0.0)
    r = tp / len(gt) if gt else 1.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return {"precision": p, "recall": r, "f1": f1, "tp": tp, "fp": len(ex - gt), "fn": len(gt - ex),
            "n_gt": len(gt), "n_extracted": len(ex)}


def percentile(values, q: float) -> float | None:
    """Linear-interpolated percentile (q in 0..100); None for an empty list."""
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def key_fact_hits(answer: str, key_facts: list[list[str]]) -> tuple[int, int]:
    """(facts matched, facts total); a fact (list of alternatives) matches if any alternative is in the answer."""
    a = answer.lower()
    return sum(1 for alts in key_facts if any(x.lower() in a for x in alts)), len(key_facts)


def citation_correct(citations: list[dict], expected_docs: list[str], expected_pages: list[int]) -> bool:
    """A cited chunk from an expected doc whose page range overlaps an expected page."""
    for c in citations:
        if expected_docs and c.get("doc_id") not in expected_docs:
            continue
        lo, hi = c.get("page_start"), c.get("page_end") or c.get("page_start")
        if lo is None:
            continue
        if any(lo <= p <= hi for p in expected_pages):
            return True
    return False


def section_correct(citations: list[dict], expected_docs: list[str], expected_section: str) -> bool:
    if not expected_section:
        return False
    return any(c.get("doc_id") in expected_docs and str(c.get("section_id") or "").startswith(expected_section)
               for c in citations)


_LABEL = re.compile(r"\[\s*[A-Z]\d+(?:\s*,\s*[A-Z]\d+)*\s*\]")


_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[])|\n+")


def sentence_groundedness(text: str, not_stated: str = "not stated in the provided documents") -> dict:
    """Share of claim sentences carrying a [C1]/[F1]/[S1] label. 'Not stated ...' sentences are not claims."""
    sents = [s.strip() for s in _SPLIT.split(text or "") if re.sub(r"[\W_]+", "", s or "")]
    claims = [s for s in sents if not_stated not in s.lower()]
    cited = [s for s in claims if _LABEL.search(s)]
    return {"claims": len(claims), "cited": len(cited), "share": (len(cited) / len(claims)) if claims else None}


def is_refusal(answer: str, mode: str) -> bool:
    low = (answer or "").lower()
    return mode == "refused" or "cannot answer" in low or "not stated in the provided documents" in low


def calibration(rows: list[dict]) -> dict:
    """rows: [{confidence, correct}] -> {level: {n, correct, accuracy}}."""
    out = {}
    for lvl in ("High", "Medium", "Low"):
        sel = [r for r in rows if r["confidence"] == lvl]
        out[lvl] = {"n": len(sel), "correct": sum(1 for r in sel if r["correct"]),
                    "accuracy": (sum(1 for r in sel if r["correct"]) / len(sel)) if sel else None}
    return out


def canon_name(n: str) -> str:
    from app.extract import normalize_name
    s = normalize_name(n)
    for suf in ("component", "composition", "swc", "interface", "ifc"):
        if s.endswith(suf) and len(s) > len(suf) + 2:
            s = s[: -len(suf)]
    return s


def name_match(gold: str, extracted: str) -> bool:
    """Tolerant name equality for real-doc gold: normalised equality, ignoring Component/Composition/SWC/Interface
    suffixes, or the 'Class::Instance' form of the extractor (gold == the part after '::')."""
    if canon_name(gold) == canon_name(extracted):
        return True
    return "::" in extracted and canon_name(gold) == canon_name(extracted.split("::")[-1])


def score_real_gold(gold_rows: list[dict], inv_by_doc: dict[str, dict]) -> dict:
    """gold_rows: [{doc_id,name,type,component,direction,page}] (verified only). Per type: recall (typed), recall
    ignoring type, page-scoped precision (extracted entities on pages that hold gold rows of that doc; gold is not
    exhaustive so this is a lower bound), F1 from those, direction accuracy of matched ports/signals."""
    out = {}
    for gtype, table in GOLD_TYPE.items():
        rows = [g for g in gold_rows if g["type"] == gtype]
        if not rows:
            continue
        hit = hit_any = ex_total = ex_ok = dir_n = dir_ok = 0
        for g in rows:
            inv = inv_by_doc.get(g["doc_id"], {})
            cands = inv.get(table, [])
            m = [e for e in cands if name_match(g["name"], e["name"])]
            hit += bool(m)
            hit_any += bool(m) or any(name_match(g["name"], e["name"]) for t in TYPES for e in inv.get(t, []))
            if m and g.get("direction") and table in ("ports",):
                dir_n += 1
                dir_ok += any(e.get("direction") == g["direction"] for e in m)
        for doc_id in {g["doc_id"] for g in rows}:
            pages = {int(g["page"]) for g in rows if g["doc_id"] == doc_id and str(g["page"]).isdigit()}
            for e in inv_by_doc.get(doc_id, {}).get(table, []):
                if e.get("page") in pages:
                    ex_total += 1
                    ex_ok += any(g["doc_id"] == doc_id and name_match(g["name"], e["name"]) for g in rows)
        rec = hit / len(rows)
        prec = ex_ok / ex_total if ex_total else None
        f1 = (2 * prec * rec / (prec + rec)) if (prec is not None and (prec + rec)) else None
        out[gtype] = {"n_gold": len(rows), "recall_typed": rec, "recall_any_type": hit_any / len(rows),
                      "precision_page_scoped": prec, "f1_page_scoped": f1, "extracted_on_gold_pages": ex_total,
                      "port_direction_accuracy": (dir_ok / dir_n) if dir_n else None}
    return out


def load_gold_csv(path: Path, verified_only: bool) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if verified_only:
        rows = [r for r in rows if (r.get("verified") or "").strip().lower() == "yes"]
    return rows


class GpuMonitor(threading.Thread):
    """Polls nvidia-smi for memory.used (MiB) while the evaluation runs."""

    def __init__(self, every: float = 2.0):
        super().__init__(daemon=True)
        self.every, self.samples, self._stop_evt, self.total = every, [], threading.Event(), None
        self.available = shutil.which("nvidia-smi") is not None

    def run(self):
        while self.available and not self._stop_evt.is_set():
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
                used, total = [float(x) for x in out.strip().splitlines()[0].split(",")]
                self.samples.append(used)
                self.total = total
            except Exception:
                pass
            self._stop_evt.wait(self.every)

    def stop(self) -> dict:
        self._stop_evt.set()
        if not self.samples:
            return {"available": self.available, "note": "nvidia-smi not available" if not self.available else "no samples"}
        return {"available": True, "total_mib": self.total, "baseline_mib": self.samples[0],
                "peak_mib": max(self.samples), "samples": len(self.samples),
                "note": f"GPU memory.used sampled every {self.every:g} s (includes any other GPU users)."}


def trace_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def lat_stats(vals: list[float]) -> dict:
    return {"n": len(vals), "p50_ms": percentile(vals, 50), "p95_ms": percentile(vals, 95),
            "mean_ms": (sum(vals) / len(vals)) if vals else None}


def say(*a):
    print(*a, flush=True)


def copy_db(name: str) -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    from app.config import settings
    dst = WORK / f"{re.sub(r'[^a-z0-9]+', '_', name.lower())}.db"
    for suf in ("", "-wal", "-shm"):
        try:
            Path(str(dst) + suf).unlink()
        except FileNotFoundError:
            pass
    shutil.copy(settings.db_path, dst)
    return dst


def extract_all(conn, model: str | None, use_llm: bool, doc_filter=None) -> dict:
    from app.extract import extract_document
    out = {}
    docs = conn.execute("SELECT project_id, doc_id, version FROM documents ORDER BY project_id, doc_id").fetchall()
    for d in docs:
        if doc_filter and d["doc_id"] not in doc_filter:
            continue
        t0 = time.perf_counter()
        stats: dict = {}
        c = extract_document(d["project_id"], d["doc_id"], conn=conn, use_llm=use_llm, model=model, stats=stats)
        out[d["doc_id"]] = {"counts": c, "seconds": round(time.perf_counter() - t0, 1), "llm_stats": stats}
        say(f"   extracted {d['doc_id']:24} {c} {time.perf_counter() - t0:.0f}s {stats or ''}")
    return out


def score_synthetic(conn, gt_path: Path, doc_ids: dict | None = None) -> dict:
    from app.extract import get_inventory
    from scripts.eval_extraction import score
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    out = {}
    for ver, d in gt["documents"].items():
        inv = get_inventory(gt["project_id"], d["doc_id"], ver, conn)
        sc = score(gt["entities"][ver], inv)
        per = {}
        for t, s in sc.items():
            r = s["recall"]
            p = s["precision"] if s["extracted"] else None  # nothing extracted: precision undefined (n/a)
            per[t] = {"precision": p, "recall": r, "f1": (2 * p * r / (p + r)) if (p and (p + r)) else 0.0,
                      "field_match": s["field_match"], "n_gt": s["gt"], "n_extracted": s["extracted"],
                      "missing": s["missing"], "extra": s["extra"]}
        tot_tp = sum(round(v["recall"] * v["n_gt"]) for v in per.values())
        tot_gt = sum(v["n_gt"] for v in per.values())
        tot_ex = sum(v["n_extracted"] for v in per.values())
        P, R = (tot_tp / tot_ex if tot_ex else None), (tot_tp / tot_gt if tot_gt else 1.0)
        per["micro_all"] = {"precision": P, "recall": R, "f1": (2 * P * R / (P + R)) if (P and (P + R)) else 0.0}
        out[d["doc_id"]] = per
    return out


def method_contribution(conn, gt_path: Path | None, project: str, doc_id: str, version: str) -> dict:
    """Per primary extraction method: how many entities, and (synthetic) how many are in the ground truth."""
    from app.extract import get_inventory
    from scripts.eval_extraction import _key
    inv = get_inventory(project, doc_id, version, conn)
    gt = None
    if gt_path:
        g = json.loads(gt_path.read_text(encoding="utf-8"))
        gt = {t: {_key(t, e) for e in g["entities"][version][t]} for t in TYPES}
    out: dict[str, dict] = {}
    for t in TYPES:
        for e in inv.get(t, []):
            m = e["extraction_method"]
            srcm = {s.get("method") or m for s in e.get("sources", [])} or {m}
            o = out.setdefault(m, {"entities": 0, "in_ground_truth": 0, "also_sourced_by": {}})
            o["entities"] += 1
            if gt is not None:
                o["in_ground_truth"] += _key(t, e) in gt[t]
            for s in srcm - {m}:
                o["also_sourced_by"][s] = o["also_sourced_by"].get(s, 0) + 1
    return out


def defects(conn, gt_path: Path, ver: str, key: str) -> dict:
    from app.checks import run_checks
    from scripts.eval_checks import evaluate
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    d = gt["documents"][ver]
    fs = run_checks(gt["project_id"], d["doc_id"], ver, conn=conn)
    r = evaluate(fs, [x for x in gt[key] if x["version"] == ver])
    r["n_planted"] = len(r["found"]) + len(r["missed"])
    r["n_findings"] = len(fs)
    return r


def diff_v1_v2(conn) -> dict:
    from app.compare import compare
    from scripts.eval_diff import evaluate
    gt = json.loads((ROOT / "data/synthetic/ground_truth.json").read_text(encoding="utf-8"))
    ed = gt["expected_diff"]
    d = compare(gt["project_id"], gt["documents"][ed["from"]]["doc_id"], ed["from"],
                gt["documents"][ed["to"]]["doc_id"], ed["to"], conn=conn)
    r = evaluate(d, ed)
    r["by_type"] = {t: {k: list(v) for k, v in kinds.items()} for t, kinds in r["by_type"].items()}
    return r


def run_qa(conn, model: str, questions: list[dict], trace_path: Path, retrieval=None) -> dict:
    from app import qa
    from app.summarize import _support, _LAB
    rows, cal_rows = [], []
    for q in questions:
        off = len(trace_entries(trace_path))
        t0 = time.perf_counter()
        try:
            ans = qa.ask(q["project_id"], q["question"], "admin", conn=conn, model=model, retrieval=retrieval)
            err = ""
        except Exception as e:  # keep evaluating
            ans, err = None, f"{type(e).__name__}: {e}"
        wall = (time.perf_counter() - t0) * 1000
        new = trace_entries(trace_path)[off:]
        gen_cached = bool(new) and all(e.get("cached") for e in new)
        if ans is None:
            rows.append({"id": q["id"], "category": q["category"], "error": err, "wall_ms": wall})
            continue
        cites = [c.model_dump() for c in ans.citations]
        refused = is_refusal(ans.answer, ans.mode)
        g = sentence_groundedness(ans.answer)
        # lexical support of each cited claim sentence by its cited chunk text
        ev = {e["label"]: e["text"] for e in ans.evidence}
        sup = []
        for s in [x for x in _SPLIT.split(ans.answer) if x.strip() and "not stated" not in x.lower()]:
            labs = [l for m in re.finditer(r"\[\s*(C\d+(?:\s*,\s*C\d+)*)\s*\]", s) for l in re.split(r"\s*,\s*", m.group(1))]
            if labs:
                sup.append(max(_support(s, ev.get(l, "")) for l in labs) >= 0.4)
        row = {"id": q["id"], "category": q["category"], "question": q["question"], "answer": ans.answer,
               "mode": ans.mode, "confidence": ans.confidence, "refused": refused, "wall_ms": wall,
               "llm_cached": gen_cached, "n_citations": len(cites), "groundedness": g,
               "lexical_support_share": (sum(sup) / len(sup)) if sup else None,
               "uncited_removed": "uncited" in (ans.limitation or "").lower(),
               "cited": [{"doc": c["doc_id"], "pages": [c["page_start"], c["page_end"]], "section": c["section_id"]}
                         for c in cites]}
        if q["category"] == "unanswerable":
            row["correct"] = refused
        else:
            hit, tot = key_fact_hits(ans.answer, q["key_facts"])
            row.update({"facts_matched": hit, "facts_total": tot,
                        "correct": (not refused) and hit == tot,
                        "citation_ok": citation_correct(cites, q["expected_docs"], q["expected_pages"]),
                        "section_ok": section_correct(cites, q["expected_docs"], q["expected_section"])})
        cal_rows.append({"confidence": ans.confidence, "correct": row["correct"]})
        rows.append(row)
        say(f"   {q['id']} {ans.mode:9} conf={ans.confidence:6} correct={row['correct']} {wall / 1000:.1f}s")
    ok = [r for r in rows if "error" not in r]
    answerable = [r for r in ok if r["category"] != "unanswerable"]
    unans = [r for r in ok if r["category"] == "unanswerable"]
    walls = [r["wall_ms"] for r in ok if not r["llm_cached"]] or [r["wall_ms"] for r in ok]
    grounded = [r["groundedness"] for r in ok if r["groundedness"]["claims"]]
    sups = [r["lexical_support_share"] for r in ok if r["lexical_support_share"] is not None]
    summary = {
        "n_questions": len(rows), "errors": len(rows) - len(ok),
        "answer_correctness": _share(answerable, "correct"),
        "key_fact_recall": (sum(r["facts_matched"] for r in answerable) / max(1, sum(r["facts_total"] for r in answerable))) if answerable else None,
        "citation_accuracy": _share(answerable, "citation_ok"),
        "section_accuracy": _share([r for r in answerable if r.get("section_ok") is not None], "section_ok"),
        "groundedness_sentences_cited": (sum(g["cited"] for g in grounded) / sum(g["claims"] for g in grounded)) if grounded else None,
        "lexical_support_share": (sum(sups) / len(sups)) if sups else None,
        "answers_with_uncited_removed": _share(ok, "uncited_removed"),
        "refusal_accuracy_unanswerable": _share(unans, "correct"),
        "false_refusal_rate_answerable": _share(answerable, "refused"),
        "calibration": calibration(cal_rows),
        "latency_wall_ms": lat_stats(walls),
        "latency_note": "end-to-end per query (retrieval + generation), uncached generations only when any exist",
    }
    return {"summary": summary, "per_question": rows}


def _share(rows: list[dict], key: str):
    return (sum(1 for r in rows if r.get(key)) / len(rows)) if rows else None


COMPONENT_SAMPLE = [("bodycomfort_r3.2.3", "Doorlocks"), ("bodycomfort_r3.2.3", "Exterior Light"),
                    ("bodycomfort_r3.2.3", "MirrorAdjust Manager"), ("chassis_r3.2.3", "ACC"),
                    ("hmi_r25-11", "Button Panel Component")]


DOC_SUMMARY_SAMPLE = ["hmi_r25-11", "chassis_r3.2.3"]


def _removed(limitation: str) -> int:
    return sum(int(x) for x in re.findall(r"(\d+)\s+uncited", limitation or ""))


def run_summaries(conn, model: str, quick: bool) -> dict:
    from app import summarize
    pid = {r["doc_id"]: r["project_id"] for r in conn.execute("SELECT project_id, doc_id FROM documents")}
    ver = {r["doc_id"]: r["version"] for r in conn.execute("SELECT doc_id, version FROM documents")}
    out = {"component_reports": [], "document_summaries": []}
    comps = COMPONENT_SAMPLE[:1] if quick else COMPONENT_SAMPLE
    for doc, name in comps:
        t0 = time.perf_counter()
        try:
            rep = summarize.component_report(pid[doc], doc, ver[doc], name, conn=conn, model=model)
        except Exception as e:
            out["component_reports"].append({"doc": doc, "component": name, "error": f"{type(e).__name__}: {e}"})
            continue
        g = sentence_groundedness(rep.get("purpose", ""))
        kept = g["claims"]
        out["component_reports"].append({
            "doc": doc, "component": name, "confidence": rep.get("confidence"), "groundedness": g,
            "raw_citation_share": kept / (kept + _removed(rep.get("limitation", ""))) if kept else 0.0,
            "uncited_removed": _removed(rep.get("limitation", "")), "seconds": round(time.perf_counter() - t0, 1),
            "purpose": rep.get("purpose", "")[:600]})
        say(f"   component_report {name}: {g}")
    for doc in ([] if quick else DOC_SUMMARY_SAMPLE):
        t0 = time.perf_counter()
        try:
            rep = summarize.document_summary(pid[doc], doc, ver[doc], conn=conn, model=model)
        except Exception as e:
            out["document_summaries"].append({"doc": doc, "error": f"{type(e).__name__}: {e}"})
            continue
        g = sentence_groundedness(rep.get("summary", ""))
        bullets = rep.get("bullets", [])
        removed = _removed(rep.get("limitation", ""))
        out["document_summaries"].append({
            "doc": doc, "confidence": rep.get("confidence"), "groundedness": g, "n_bullets": len(bullets),
            "bullets_cited_share": (sum(1 for b in bullets if _LABEL.search(b["text"] + f" [{b['label']}]")) / len(bullets)) if bullets else None,
            "raw_citation_share": (len(bullets) / (len(bullets) + removed)) if (bullets or removed) else None,
            "uncited_removed": removed, "seconds": round(time.perf_counter() - t0, 1),
            "summary": rep.get("summary", "")[:600]})
        say(f"   document_summary {doc}: {g}")
    ok = [r for r in out["component_reports"] if "error" not in r and r["groundedness"]["share"] is not None]
    okd = [r for r in out["document_summaries"] if "error" not in r and r["groundedness"]["share"] is not None]
    out["summary"] = {
        "component_groundedness": (sum(r["groundedness"]["cited"] for r in ok) / max(1, sum(r["groundedness"]["claims"] for r in ok))) if ok else None,
        "component_raw_citation_share": (sum(r["raw_citation_share"] for r in ok) / len(ok)) if ok else None,
        "n_component_reports": len(out["component_reports"]), "n_component_ok": len(ok),
        "document_groundedness": (sum(r["groundedness"]["cited"] for r in okd) / max(1, sum(r["groundedness"]["claims"] for r in okd))) if okd else None,
        "document_raw_citation_share": (sum(r["raw_citation_share"] for r in okd if r["raw_citation_share"] is not None) / len(okd)) if okd else None,
        "n_document_summaries": len(out["document_summaries"]), "n_document_ok": len(okd)}
    return out


def run_controls(db: Path, model: str) -> dict:
    from app import controls, retrieve
    from app.ingest import IngestRejected, ingest_document
    import tempfile
    res: dict = {}
    conn = controls.get_conn(db)
    # 1 unapproved upload rejected
    tmp = Path(tempfile.mkdtemp(prefix="eval_ctl_"))
    try:
        ingest_document(ROOT / "data/synthetic/hld_v1.pdf", "synthetic-ecu", "hld", "9.9", "unapproved", "admin",
                        doc_id="ctl_unapproved", conn=conn, processed_dir=tmp / "p", chroma_dir=tmp / "c", index=False)
        res["unapproved_upload_rejected"] = {"passed": False, "detail": "ingest accepted an unapproved source"}
    except IngestRejected as e:
        res["unapproved_upload_rejected"] = {"passed": True, "detail": f"IngestRejected: {e}"}
    except Exception as e:
        res["unapproved_upload_rejected"] = {"passed": False, "detail": f"{type(e).__name__}: {e}"}
    # 2 cross-project isolation: outsider cannot ask; project search never returns another project's documents
    from app import qa
    controls.add_user("eval_outsider", "viewer", conn=conn)
    try:
        qa.ask("hmi", "What does the ButtonPanel represent?", "eval_outsider", conn=conn, model=model)
        denied = False
    except controls.PermissionDenied:
        denied = True
    hits = retrieve.search("chassis", "InterDomainController ButtonPanel PDC Button SWC", k=8)
    foreign = [h["metadata"].get("doc_id") for h in hits if not str(h["metadata"].get("doc_id", "")).startswith("chassis")]
    res["cross_project_query_empty"] = {
        "passed": denied and not foreign,
        "detail": f"non-member ask denied={denied}; chassis search for HMI-only terms returned {len(hits)} hits, "
                  f"{len(foreign)} from other projects"}
    # 3 viewer cannot approve
    fid = conn.execute("SELECT id FROM findings LIMIT 1").fetchone()
    try:
        controls.review("finding", fid["id"] if fid else 1, "approved", "eval", "viewer", project_id="synthetic-ecu", conn=conn)
        res["viewer_cannot_approve"] = {"passed": False, "detail": "viewer approval succeeded"}
    except controls.PermissionDenied as e:
        res["viewer_cannot_approve"] = {"passed": True, "detail": f"PermissionDenied: {e}"}
    except Exception as e:
        res["viewer_cannot_approve"] = {"passed": False, "detail": f"{type(e).__name__}: {e}"}
    conn.close()
    # 4 offline run (separate process with HF offline flags)
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "DB_PATH": str(db),
           "PYTHONIOENCODING": "utf-8"}
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--offline-child", "--model", model],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=900,
                       cwd=str(ROOT))
    line = next((l for l in p.stdout.splitlines()[::-1] if l.startswith("OFFLINE_RESULT ")), "")
    child = json.loads(line[len("OFFLINE_RESULT "):]) if line else {}
    res["offline_run_works"] = {"passed": bool(child.get("ask_ok") and child.get("extract_ok")) and p.returncode == 0,
                                "detail": child or {"stderr_tail": p.stderr[-400:]},
                                "seconds": round(time.perf_counter() - t0, 1)}
    return res


def offline_child(model: str) -> int:
    from app import controls, qa
    from app.extract import extract_from_prose
    from app.ingest import load_processed
    conn = controls.get_conn()
    out = {"HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE"), "TRANSFORMERS_OFFLINE": os.environ.get("TRANSFORMERS_OFFLINE")}
    try:
        a = qa.ask("hmi", "What does the ButtonPanel represent?", "admin", conn=conn, model=model)
        out["ask_ok"] = a.mode in ("generated", "refused") and bool(a.answer)
        out["ask_mode"] = a.mode
    except Exception as e:
        out["ask_ok"], out["ask_error"] = False, f"{type(e).__name__}: {e}"
    try:
        doc = load_processed("body-comfort", "bodycomfort_r3.2.3")
        sec = next(s for s in doc["sections"] if s["section_id"] == "4.1")
        ents = extract_from_prose(sec, "bodycomfort_r3.2.3", "R3.2.3", model=model)
        out["extract_ok"], out["extract_entities"] = True, len(ents)
    except Exception as e:
        out["extract_ok"], out["extract_error"] = False, f"{type(e).__name__}: {e}"
    print("OFFLINE_RESULT " + json.dumps(out))
    return 0


def inventories(conn) -> dict[str, dict]:
    from app.extract import get_inventory
    out = {}
    for d in conn.execute("SELECT project_id, doc_id, version FROM documents"):
        out[d["doc_id"]] = get_inventory(d["project_id"], d["doc_id"], d["version"], conn)
    return out


def pct(x, d=0):
    return "n/a" if x is None else f"{100 * x:.{d}f}%"


def f2(x):
    return "n/a" if x is None else f"{x:.2f}"


def ms(x):
    return "n/a" if x is None else (f"{x / 1000:.1f} s" if x >= 1000 else f"{x:.0f} ms")


def table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def review_effort_summary(path: Path | None = None) -> dict:
    """Manual vs tool review minutes from eval/review_effort.json (written by scripts/review_timer.py)."""
    from datetime import datetime
    p = path or EVAL_DIR / "review_effort.json"
    if not Path(p).exists():
        return {"status": "not measured"}
    ph = json.loads(Path(p).read_text(encoding="utf-8")).get("phases", {})

    def mins(x: dict) -> float:
        return sum((datetime.fromisoformat(i["stop"]) - datetime.fromisoformat(i["start"])).total_seconds() / 60
                   for i in x.get("intervals", []) if i.get("stop"))

    m, t = ph.get("manual", {}), ph.get("tool", {})
    mm, tm = mins(m), mins(t)
    if mm <= 0 or tm <= 0:
        return {"status": "not measured"}
    return {"status": "measured", "manual_minutes": round(mm, 1), "tool_minutes": round(tm, 1),
            "reduction_pct": round(100 * (mm - tm) / mm, 1), "manual_found": m.get("found"), "tool_found": t.get("found")}


def _review_effort_md(RE: dict) -> list[str]:
    if RE.get("status") != "measured":
        return ["## Review effort", "", "Not measured (see docs/review_effort_protocol.md).", ""]
    return ["## Review effort", "",
            f"Manual {RE['manual_minutes']} min vs tool {RE['tool_minutes']} min: {RE['reduction_pct']}% reduction. "
            f"Defects found: manual {RE['manual_found']}, tool {RE['tool_found']} (n=1 author, see protocol caveats).", ""]


def latency_from_traces(model: str, entries: list[dict]) -> dict:
    """Latency of non-cached LLM calls of one model in a slice of data/traces.jsonl: Q&A generation (purposes qa,
    qa_desc), one extraction call (extract_prose) and per section (sum of the calls of one section)."""
    ents = [e for e in entries if e.get("model") == model and e.get("ok", True) and not e.get("cached")]
    by: dict[str, list[float]] = {}
    for e in ents:
        by.setdefault(e.get("purpose", ""), []).append(float(e["latency_ms"]))
    sec: dict[str, float] = {}
    for e in ents:
        if e.get("purpose") == "extract_prose" and e.get("chunk_ids"):
            doc, sid, _ = (e["chunk_ids"][0].split(":") + ["", ""])[:3]
            sec[f"{doc}:{sid}"] = sec.get(f"{doc}:{sid}", 0.0) + float(e["latency_ms"])
    return {"qa_generation": lat_stats(by.get("qa", []) + by.get("qa_desc", [])),
            "extract_per_call": lat_stats(by.get("extract_prose", [])),
            "extract_per_section": lat_stats(list(sec.values())),
            "by_purpose": {k: lat_stats(v) for k, v in by.items()}}


# ====================================================================== protocol (written before the models run)

REAL_DOCS = ("bodycomfort_r3.2.3", "hmi_r25-11")
V4_DOC, V4_VER, V4_GT = "syn_hld_v4_heldout", "4.0", ROOT / "data/synthetic/ground_truth_v4.json"
BASELINE, CANDIDATE = "qwen2.5:3b", "qwen3:4b-instruct"
ONE_QUESTION = 0.04  # 1 of 25 gold questions
RULE = {"qa_gain": 0.08, "f1_gain": 0.05, "p95_ms": 20000, "gpu_share": 0.9}
DECISION_RULE = [
    f"Switch the default model from {BASELINE} to {CANDIDATE} only if ALL of:",
    "1. (gold Q&A answer correctness of the candidate >= baseline + 8 points, OR mean real-gold extraction F1 >= "
    "baseline + 0.05; mean over the entity types whose F1 is defined), measured on the first (cold) run;",
    "2. refusal accuracy on the unanswerable gold questions does not drop;",
    "3. Q&A end-to-end latency p95 of the candidate (second, warm run) stays under 20 s;",
    "4. the candidate fits in the 4 GB GPU without heavy CPU spill: `ollama ps` shows >= 90% of the model on the GPU.",
    "Otherwise keep the baseline. One question = 4 points; differences of 4 points or less are 'no real difference'.",
]
PROTOCOL = [
    "Same retrieval (hybrid default), prompts, documents, thresholds and machine (RTX 3050 4 GB) for both models.",
    "The LLM cache is keyed by model, so the models never share answers. Each model runs the whole evaluation twice "
    "with cache reads off (LLM_CACHE_READ=0): run 1 starts after unloading every model (cold start measured), run 2 "
    "right after (warm). Quality numbers come from run 1; latency from run 2; answers that changed between the runs "
    "are listed (sampling noise, temperature 0.1).",
    "Extraction is run once per model and run on BodyComfort R3.2.3, HMI R25-11 and the frozen held-out v4 document "
    "(sha256 checked against eval/heldout_v4.lock before anything runs). The deterministic (no LLM) baseline runs once.",
    "Real-document precision is page-scoped: extracted entities on the pages that carry gold rows of that type "
    "(eval/real_gold.csv is not exhaustive), so it is a lower bound.",
]


def protocol_md() -> list[str]:
    return (["## Protocol and decision rule (written before the models were run)", ""] + [f"- {x}" for x in PROTOCOL]
            + ["", "Decision rule:", ""] + DECISION_RULE + [""])


def write_protocol() -> None:
    (EVAL_DIR / "results.md").write_text("# Evaluation results\n\nRun in progress; only the protocol is fixed so "
                                         "far.\n\n" + "\n".join(protocol_md()), encoding="utf-8")


def verify_v4_lock() -> dict:
    """sha256 of the frozen v4 files against eval/heldout_v4.lock; raises SystemExit when one differs."""
    import hashlib
    lock = (EVAL_DIR / "heldout_v4.lock").read_text(encoding="utf-8")
    want: dict[str, list[str]] = {}
    for line in lock.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{64}", parts[1]):
            want[parts[0]] = [parts[1]] + re.findall(r"[0-9a-f]{64}", " ".join(parts[2:]))
    out = {}
    for name, hashes in want.items():
        p = ROOT / "data/synthetic" / name
        raw = p.read_bytes()
        got = {hashlib.sha256(raw).hexdigest(), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()}
        ok = bool(got & set(hashes))
        out[name] = {"ok": ok, "sha256": hashlib.sha256(raw).hexdigest()}
        if not ok:
            raise SystemExit(f"ABORT: {name} does not match eval/heldout_v4.lock (held-out v4 changed)")
    return out


# ====================================================================== Ollama helpers (cold start, placement)

def _ollama(path: str, payload: dict | None = None, timeout: float = 600) -> dict:
    import requests
    from app.config import settings
    url = settings.ollama_base_url.rstrip("/") + path
    r = requests.post(url, json=payload, timeout=timeout) if payload is not None else requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def unload_all() -> None:
    for m in _ollama("/api/ps").get("models", []):
        _ollama("/api/generate", {"model": m["name"], "keep_alive": 0})
    for _ in range(30):
        if not _ollama("/api/ps").get("models"):
            return
        time.sleep(1)


def cold_start(model: str) -> dict:
    """Unload every model, then time a 1-token generation (load + first token) with num_ctx 4096."""
    from app.llm import OPTIONS, thinking_off
    unload_all()
    t0 = time.perf_counter()
    payload = {"model": model, "prompt": "OK", "stream": False, "options": {**OPTIONS, "num_predict": 1}}
    if thinking_off(model):
        payload["think"] = False
    body = _ollama("/api/generate", payload)
    return {"wall_ms": (time.perf_counter() - t0) * 1000, "load_ms": body.get("load_duration", 0) / 1e6}


def placement(model: str) -> dict:
    """`ollama ps` for the loaded model: size, size on GPU, share on GPU."""
    for m in _ollama("/api/ps").get("models", []):
        if m["name"] == model:
            size, vram = m.get("size") or 0, m.get("size_vram") or 0
            share = vram / size if size else None
            return {"size_mib": round(size / 2**20), "gpu_mib": round(vram / 2**20), "gpu_share": share,
                    "processor": "100% GPU" if share and share >= 0.999 else
                    f"{100 * (1 - share):.0f}%/{100 * share:.0f}% CPU/GPU" if share is not None else "n/a"}
    return {"note": "model not loaded at the time of the check"}


# ====================================================================== blocks A-D (per model run), E, F

def extraction_blocks(conn, model: str | None, real_gold: list[dict]) -> dict:
    """A (real gold) and B (held-out v4: P/R/F1 + planted defects) for one extraction run (model None = no LLM)."""
    from app.extract import get_inventory
    t0 = time.perf_counter()
    ext = extract_all(conn, model, model is not None, set(REAL_DOCS) | {V4_DOC})
    inv = {d: get_inventory(r["project_id"], d, None, conn) for d in REAL_DOCS
           for r in conn.execute("SELECT project_id FROM documents WHERE doc_id=?", (d,))}
    real = score_real_gold(real_gold, inv)
    v4 = score_synthetic(conn, V4_GT)[V4_DOC]
    return {"A_real": real, "A_mean_f1": _mean_f1(real), "B_v4": v4,
            "B_defects": defects(conn, V4_GT, V4_VER, "planted_defects"),
            "entity_counts": {d: {t: len(v.get(t, [])) for t in TYPES} for d, v in inv.items()},
            "extraction_seconds": round(time.perf_counter() - t0, 1), "runs": ext}


def _mean_f1(real: dict) -> float | None:
    f = [v["f1_page_scoped"] for v in real.values() if v.get("f1_page_scoped") is not None]
    return (sum(f) / len(f)) if f else None


def deterministic_baseline(real_gold: list[dict]) -> dict:
    from app import controls
    say("\n=== deterministic baseline (no LLM)")
    conn = controls.get_conn(copy_db("deterministic"))
    out = extraction_blocks(conn, None, real_gold)
    conn.close()
    return out


def run_model(model: str, run: int, questions: list[dict], real_gold: list[dict], trace_path: Path) -> dict:
    """One full evaluation run of one model (blocks A-D) on its own DB copy, cache reads off."""
    from app import controls
    say(f"\n=== MODEL {model} run {run}")
    os.environ["LLM_CACHE_READ"] = "0"
    db = copy_db(f"{model}_run{run}")
    conn = controls.get_conn(db)
    mon = GpuMonitor(every=2.0)
    mon.start()
    off = len(trace_entries(trace_path))
    t0 = time.perf_counter()
    res: dict = {"model": model, "run": run, "db": str(db)}
    res.update(extraction_blocks(conn, model, real_gold))
    res["placement"] = placement(model)
    say(f"   A mean F1 {res['A_mean_f1']}, B micro F1 {res['B_v4']['micro_all']['f1']:.2f}, {res['placement']}")
    res["C_qa"] = run_qa(conn, model, questions, trace_path)
    res["D_summaries"] = run_summaries(conn, model, quick=False)
    res["wall_seconds"] = round(time.perf_counter() - t0)
    res["vram"] = mon.stop()
    res["latency"] = latency_from_traces(model, trace_entries(trace_path)[off:])
    conn.close()
    os.environ.pop("LLM_CACHE_READ", None)
    return res


def changed_answers(r1: dict, r2: dict) -> list[dict]:
    a = {x["id"]: x for x in r1["C_qa"]["per_question"]}
    out = []
    for x in r2["C_qa"]["per_question"]:
        y = a.get(x["id"], {})
        if x.get("answer") != y.get("answer"):
            out.append({"id": x["id"], "correct_run1": y.get("correct"), "correct_run2": x.get("correct")})
    return out


def decide(R: dict) -> dict:
    """Apply the decision rule (DECISION_RULE) to the run-1 quality and run-2 latency of both models."""
    M = R["models"]
    if BASELINE not in M or CANDIDATE not in M:
        return {"switch": False, "default": BASELINE, "reason": "both models are needed for the decision rule",
                "checks": {}}
    b, c = M[BASELINE]["run1"], M[CANDIDATE]["run1"]
    qa_d = c["C_qa"]["summary"]["answer_correctness"] - b["C_qa"]["summary"]["answer_correctness"]
    f1_d = (c["A_mean_f1"] or 0) - (b["A_mean_f1"] or 0)
    ref_ok = (c["C_qa"]["summary"]["refusal_accuracy_unanswerable"] or 0) >= \
        (b["C_qa"]["summary"]["refusal_accuracy_unanswerable"] or 0)
    p95 = M[CANDIDATE]["run2"]["C_qa"]["summary"]["latency_wall_ms"]["p95_ms"]
    share = (M[CANDIDATE].get("placement") or {}).get("gpu_share")
    checks = {"quality_gain": qa_d >= RULE["qa_gain"] - 1e-9 or f1_d >= RULE["f1_gain"] - 1e-9,
              "refusal_not_lower": ref_ok, "p95_under_20s": p95 is not None and p95 < RULE["p95_ms"],
              "fits_gpu": share is not None and share >= RULE["gpu_share"]}
    switch = all(checks.values())
    noise = "no real difference" if abs(qa_d) <= ONE_QUESTION + 1e-9 else ("better" if qa_d > 0 else "worse")
    reason = (f"Q&A correctness {CANDIDATE} - {BASELINE} = {qa_d * 100:+.0f} points ({noise}); real-gold mean F1 "
              f"{f1_d:+.2f}; refusal accuracy {'not lower' if ref_ok else 'LOWER'}; {CANDIDATE} Q&A p95 "
              f"{(p95 or 0) / 1000:.1f} s; on GPU {('%.0f%%' % (100 * share)) if share is not None else 'n/a'}.")
    return {"switch": switch, "default": CANDIDATE if switch else BASELINE, "checks": checks,
            "qa_delta": qa_d, "f1_delta": f1_d, "reason": reason,
            "failed": [k for k, v in checks.items() if not v]}


# ====================================================================== rendering

def _prf_cell(d: dict | None) -> str:
    if not d:
        return "n/a"
    return f"P {f2(d.get('precision'))} / R {f2(d.get('recall'))} / F1 {f2(d.get('f1'))}"


def _real_cell(v: dict | None) -> str:
    if not v:
        return "n/a"
    return (f"P {f2(v['precision_page_scoped'])} / R {f2(v['recall_typed'])} / F1 {f2(v['f1_page_scoped'])}")


def render_md(R: dict) -> str:
    models = list(R["models"])
    det = R["deterministic"]
    r1 = {m: R["models"][m]["run1"] for m in models}
    r2 = {m: R["models"][m]["run2"] for m in models}
    L = ["# Evaluation results", "",
         f"Generated {R['meta']['generated']} | models: {', '.join(models)} | all local (Ollama, RTX 3050 4 GB) | "
         f"wall time {R['meta']['wall_seconds'] // 60} min. Gold sets were not used for development "
         "(dev set: eval/dev_results.md).", ""]
    L += protocol_md()
    d = R["decision"]
    L += ["## Decision", "", f"**Default model: {d['default']}** ({'switch' if d['switch'] else 'no switch'}).",
          d["reason"],
          ("All four conditions hold." if d["switch"] else "Failed conditions: " + ", ".join(d.get("failed") or ["-"])
           + "."), ""]
    # A
    L += ["## A. Real-document extraction (51 human-verified rows, eval/real_gold.csv)", "",
          "BodyComfort R3.2.3 and HMI R25-11, one extraction per model. Recall over gold rows of that type; precision "
          "page-scoped (lower bound).", ""]
    gtypes = [t for t in GOLD_TYPE if t in det["A_real"]]
    rows = [[f"{t} (n={det['A_real'][t]['n_gold']})", _real_cell(det["A_real"].get(t))]
            + [_real_cell(r1[m]["A_real"].get(t)) for m in models] for t in gtypes]
    rows.append(["mean F1 (types with defined F1)", f2(det["A_mean_f1"])] + [f2(r1[m]["A_mean_f1"]) for m in models])
    L += [table(["entity type", "deterministic (no LLM)"] + models, rows), ""]
    # B
    L += ["## B. Held-out v4 extraction and planted defects (frozen; sha256 verified against eval/heldout_v4.lock)", ""]
    rows = [[t, _prf_cell(det["B_v4"].get(t))] + [_prf_cell(r1[m]["B_v4"].get(t)) for m in models]
            for t in list(TYPES) + ["micro_all"]]
    dd = lambda x: (f"found {len(x['found'])}/{x['n_planted']}, missed {', '.join(x['missed']) or '-'}, "  # noqa: E731
                    f"FP {len(x['false_positives'])}")
    rows.append(["planted defects", dd(det["B_defects"])] + [dd(r1[m]["B_defects"]) for m in models])
    L += [table(["entity type", "deterministic (no LLM)"] + models, rows), ""]
    for m in models:
        fp = r1[m]["B_defects"]["false_positives"]
        if fp:
            L.append(f"- {m} defect false positives: {', '.join(map(str, fp))}")
    L.append("")
    # C
    keys = [("answer correctness (all key facts present)", "answer_correctness", pct),
            ("key-fact recall", "key_fact_recall", pct), ("citation accuracy (doc + page)", "citation_accuracy", pct),
            ("citation accuracy (section)", "section_accuracy", pct),
            ("groundedness (claim sentences cited)", "groundedness_sentences_cited", pct),
            ("refusal accuracy (5 unanswerable)", "refusal_accuracy_unanswerable", pct),
            ("false-refusal rate (20 answerable)", "false_refusal_rate_answerable", pct)]
    L += ["## C. Q&A (25 gold questions: 12 real, 8 synthetic, 5 unanswerable; run 1)", ""]
    rows = [[lab] + [f(r1[m]["C_qa"]["summary"].get(k)) for m in models] for lab, k, f in keys]
    for lvl in ("High", "Medium", "Low"):
        rows.append([f"calibration: accuracy when {lvl}"] + [
            (lambda c: f"{c['correct']}/{c['n']} = {pct(c['accuracy'])}" if c["n"] else "-")(
                r1[m]["C_qa"]["summary"]["calibration"][lvl]) for m in models])
    rows.append(["answers changed in run 2 (sampling noise)"] + [
        (", ".join(f"{x['id']} ({'ok' if x['correct_run1'] else 'wrong'}->{'ok' if x['correct_run2'] else 'wrong'})"
                   for x in R["models"][m]["changed"]) or "none") for m in models])
    rows.append(["answer correctness in run 2"] + [pct(r2[m]["C_qa"]["summary"]["answer_correctness"]) for m in models])
    L += [table(["metric"] + models, rows), ""]
    per = []
    for q in r1[models[0]]["C_qa"]["per_question"]:
        cells = []
        for m in models:
            x = next((y for y in r1[m]["C_qa"]["per_question"] if y["id"] == q["id"]), {})
            cells.append(f"{'ok' if x.get('correct') else 'WRONG'} / {x.get('confidence', '-')}")
        per.append([q["id"]] + cells)
    L += ["Per question (correct / confidence):", "", table(["question"] + models, per), ""]
    # D
    L += ["## D. Cited summaries (5 component reports, 2 document summaries)", ""]
    sk = [("component reports: claim sentences cited (final)", "component_groundedness", pct),
          ("component reports: cited before the filter", "component_raw_citation_share", pct),
          ("document summaries: claim sentences cited (final)", "document_groundedness", pct),
          ("document summaries: cited before the filter", "document_raw_citation_share", pct),
          ("component reports produced", "n_component_ok", str), ("document summaries produced", "n_document_ok", str)]
    L += [table(["metric"] + models, [[lab] + [f(r1[m]["D_summaries"]["summary"].get(k)) for m in models]
                                      for lab, k, f in sk]), ""]
    # E
    L += [f"## E. Controls (model independent; run once with {R['controls']['model']})", "",
          table(["control", "result", "detail"],
                [[k, "PASS" if v["passed"] else "FAIL", str(v["detail"])[:160].replace("|", "/")]
                 for k, v in R["controls"]["results"].items()]), ""]
    # F
    L += ["## F. Speed and memory (latency from run 2 = warm; cold start before run 1)", ""]
    lat = lambda m, k: r2[m]["latency"][k]  # noqa: E731
    rows = [["Q&A end-to-end p50 / p95"] + [f"{ms(r2[m]['C_qa']['summary']['latency_wall_ms']['p50_ms'])} / "
                                           f"{ms(r2[m]['C_qa']['summary']['latency_wall_ms']['p95_ms'])}" for m in models],
            ["Q&A generation call p50 / p95"] + [f"{ms(lat(m, 'qa_generation')['p50_ms'])} / "
                                                f"{ms(lat(m, 'qa_generation')['p95_ms'])} (n={lat(m, 'qa_generation')['n']})"
                                                for m in models],
            ["extraction per section p50 / p95"] + [f"{ms(lat(m, 'extract_per_section')['p50_ms'])} / "
                                                   f"{ms(lat(m, 'extract_per_section')['p95_ms'])} "
                                                   f"(n={lat(m, 'extract_per_section')['n']})" for m in models],
            ["cold start (load + first token)"] + [f"{ms(R['models'][m]['cold_start']['wall_ms'])} (load "
                                                  f"{ms(R['models'][m]['cold_start']['load_ms'])})" for m in models],
            ["`ollama ps` placement"] + [f"{(R['models'][m].get('placement') or {}).get('processor', 'n/a')}, "
                                        f"{(R['models'][m].get('placement') or {}).get('size_mib', '?')} MiB"
                                        for m in models],
            ["peak GPU memory used (nvidia-smi, every 2 s)"] + [
                f"{r2[m]['vram'].get('peak_mib', 'n/a')} of {r2[m]['vram'].get('total_mib', '?')} MiB (idle "
                f"{R['meta'].get('gpu_idle_mib', '?')} MiB)" for m in models],
            ["fits fully on the 4 GB GPU"] + [
                ("yes" if ((R['models'][m].get('placement') or {}).get('gpu_share') or 0) >= 0.999 else "no")
                for m in models],
            ["wall time of one full run (run 2)"] + [f"{r2[m]['wall_seconds'] // 60} min" for m in models]]
    L += [table(["metric"] + models, rows), "",
          "GPU memory includes other processes on the GPU (idle value above); only one model is resident at a time.",
          ""]
    if R.get("thinking"):
        L += [f"qwen3 tag: {R['thinking']}", ""]
    L += ["Files: eval/results.json (all numbers), eval/charts/*.png, eval/run.log."]
    return "\n".join(L) + "\n"


def make_charts(R: dict) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    CHARTS.mkdir(parents=True, exist_ok=True)
    for old in CHARTS.glob("*.png"):
        old.unlink()
    models = list(R["models"])
    series = [("deterministic", R["deterministic"])] + [(m, R["models"][m]["run1"]) for m in models]
    colors = ["#8a8a8a", "#2b6cb0", "#dd6b20", "#2f855a"]
    files = []
    # 1 extraction F1 by type: real gold (A) and held-out v4 (B)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
    gtypes = [t for t in GOLD_TYPE if t in R["deterministic"]["A_real"]]
    for ax, title, keys, get in (
            (axes[0], "A. Real gold (page-scoped F1)", gtypes,
             lambda s, t: s["A_real"].get(t, {}).get("f1_page_scoped")),
            (axes[1], "B. Held-out v4 F1", list(TYPES), lambda s, t: s["B_v4"].get(t, {}).get("f1"))):
        w = 0.8 / len(series)
        for i, (name, s) in enumerate(series):
            ax.bar([j + i * w for j in range(len(keys))], [get(s, t) or 0 for t in keys], w, label=name,
                   color=colors[i % len(colors)])
        ax.set_xticks([j + 0.4 - w / 2 for j in range(len(keys))])
        ax.set_xticklabels(keys)
        ax.set_title(title)
        ax.set_ylim(0, 1.05)
    axes[0].set_ylabel("F1")
    axes[1].legend(fontsize=8)
    fig.tight_layout()
    p = CHARTS / "extraction_f1.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    files.append(p.name)
    # 2 Q&A metrics
    keys = [("answer_correctness", "correct"), ("key_fact_recall", "key facts"), ("citation_accuracy", "citation"),
            ("groundedness_sentences_cited", "grounded"), ("refusal_accuracy_unanswerable", "refusal")]
    fig, ax = plt.subplots(figsize=(8, 4))
    w = 0.8 / len(models)
    for i, m in enumerate(models):
        s = R["models"][m]["run1"]["C_qa"]["summary"]
        ax.bar([j + i * w for j in range(len(keys))], [s.get(k) or 0 for k, _ in keys], w, label=m,
               color=colors[i + 1])
    ax.set_xticks([j + 0.4 - w / 2 for j in range(len(keys))])
    ax.set_xticklabels([lab for _, lab in keys])
    ax.set_ylim(0, 1.05)
    ax.set_title("C. Gold Q&A (25 questions; 1 question = 4 points)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = CHARTS / "qa_metrics.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    files.append(p.name)
    # 3 latency and VRAM
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    lk = [("Q&A e2e p95", lambda m: R["models"][m]["run2"]["C_qa"]["summary"]["latency_wall_ms"]["p95_ms"]),
          ("Q&A gen p95", lambda m: R["models"][m]["run2"]["latency"]["qa_generation"]["p95_ms"]),
          ("extract/section p95", lambda m: R["models"][m]["run2"]["latency"]["extract_per_section"]["p95_ms"]),
          ("cold start", lambda m: R["models"][m]["cold_start"]["wall_ms"])]
    for i, m in enumerate(models):
        axes[0].bar([j + i * w for j in range(len(lk))], [(f(m) or 0) / 1000 for _, f in lk], w, label=m,
                    color=colors[i + 1])
    axes[0].axhline(20, color="#c53030", lw=1, ls="--")
    axes[0].set_xticks([j + 0.4 - w / 2 for j in range(len(lk))])
    axes[0].set_xticklabels([lab for lab, _ in lk], fontsize=8)
    axes[0].set_ylabel("seconds")
    axes[0].set_title("F. Latency (warm run; dashed = 20 s limit)")
    axes[0].legend(fontsize=8)
    pl = [R["models"][m].get("placement") or {} for m in models]
    axes[1].bar(models, [x.get("gpu_mib") or 0 for x in pl], color="#2f855a", label="on GPU")
    axes[1].bar(models, [(x.get("size_mib") or 0) - (x.get("gpu_mib") or 0) for x in pl],
                bottom=[x.get("gpu_mib") or 0 for x in pl], color="#c05621", label="on CPU")
    axes[1].axhline(4096, color="#c53030", lw=1, ls="--")
    axes[1].set_ylabel("MiB")
    axes[1].set_title("F. Model placement (ollama ps; dashed = 4 GB)")
    axes[1].legend(fontsize=8)
    fig.tight_layout()
    p = CHARTS / "latency_vram.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    files.append(p.name)
    return files


def log(line: str) -> None:
    say(line)
    with open(EVAL_DIR / "run.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _slim(R: dict) -> dict:
    """results.json without the per-run DB paths and raw extraction stats that only matter for debugging."""
    return json.loads(json.dumps(R, default=str))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=[BASELINE, CANDIDATE])
    ap.add_argument("--render-only", action="store_true", help="re-render results.md and charts from results.json")
    ap.add_argument("--offline-child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--model", default=BASELINE, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.offline_child:
        return offline_child(a.model)
    if a.render_only:
        R = json.loads((EVAL_DIR / "results.json").read_text(encoding="utf-8"))
        R["charts"] = make_charts(R)
        (EVAL_DIR / "results.md").write_text(render_md(R), encoding="utf-8")
        return 0
    allowed = [m.strip() for m in os.environ.get("ALLOWED_MODELS", BASELINE).split(",") if m.strip()]
    os.environ["ALLOWED_MODELS"] = ",".join(dict.fromkeys(allowed + a.models))  # before app.config loads
    from app.config import settings
    from app.llm import validate_model
    for m in a.models:
        validate_model(m)
    t_start = time.time()
    log(f"\n## run_eval {time.strftime('%Y-%m-%d %H:%M')}: models {a.models}")
    write_protocol()
    lock = verify_v4_lock()
    log(f"v4 lock verified: {lock}")
    questions = json.loads((EVAL_DIR / "gold_questions.json").read_text(encoding="utf-8"))["questions"]
    real_gold = load_gold_csv(EVAL_DIR / "real_gold.csv", verified_only=True)
    log(f"gold: {len(questions)} questions, {len(real_gold)} verified real-gold rows")
    idle = GpuMonitor(every=2.0)
    unload_all()
    idle.start()
    time.sleep(4)
    idle_v = idle.stop()
    R: dict = {"meta": {"generated": time.strftime("%Y-%m-%d %H:%M"), "n_questions": len(questions),
                        "n_real_gold": len(real_gold), "v4_lock": lock, "gpu_idle_mib": idle_v.get("peak_mib"),
                        "protocol": PROTOCOL, "decision_rule": DECISION_RULE},
               "thinking": (f"{CANDIDATE} lists the 'thinking' capability in Ollama; every call sends think=false "
                            "and <think>...</think> blocks are stripped before JSON/citation parsing (none were "
                            "observed in a manual check)."),
               "models": {}}
    t0 = time.perf_counter()
    R["deterministic"] = deterministic_baseline(real_gold)
    log(f"deterministic baseline {time.perf_counter() - t0:.0f}s: A mean F1 {R['deterministic']['A_mean_f1']}")
    for m in a.models:
        cs = cold_start(m)
        log(f"{m}: cold start {cs['wall_ms'] / 1000:.1f}s (load {cs['load_ms'] / 1000:.1f}s)")
        runs = {}
        for run in (1, 2):
            t0 = time.perf_counter()
            try:
                runs[f"run{run}"] = run_model(m, run, questions, real_gold, settings.trace_path)
            except Exception as e:  # keep the other model's results; the error goes to run.log
                log(f"ERROR {m} run {run}: {type(e).__name__}: {e}")
                raise
            q = runs[f"run{run}"]["C_qa"]["summary"]
            log(f"{m} run {run}: {time.perf_counter() - t0:.0f}s, Q&A correct {q['answer_correctness']:.2f}, "
                f"refusal {q['refusal_accuracy_unanswerable']}, p95 {q['latency_wall_ms']['p95_ms']}, "
                f"A mean F1 {runs[f'run{run}']['A_mean_f1']}")
        R["models"][m] = {**runs, "cold_start": cs, "placement": runs["run2"]["placement"],
                          "changed": changed_answers(runs["run1"], runs["run2"])}
        log(f"{m}: placement {R['models'][m]['placement']}; changed answers run1->run2: {R['models'][m]['changed']}")
    log("controls (model independent)")
    first = a.models[0]
    R["controls"] = {"model": first, "results": run_controls(Path(R["models"][first]["run1"]["db"]), first)}
    for k, v in R["controls"]["results"].items():
        log(f"   {k}: {'PASS' if v['passed'] else 'FAIL'}")
    R["decision"] = decide(R)
    R["meta"]["wall_seconds"] = round(time.time() - t_start)
    R["charts"] = make_charts(R)
    (EVAL_DIR / "results.json").write_text(json.dumps(_slim(R), indent=2, ensure_ascii=False), encoding="utf-8")
    (EVAL_DIR / "results.md").write_text(render_md(R), encoding="utf-8")
    log(f"decision: default {R['decision']['default']} (switch={R['decision']['switch']}); {R['decision']['reason']}")
    log(f"wrote eval/results.json, eval/results.md, charts {R['charts']}; wall {R['meta']['wall_seconds']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
