"""Synthetic-only evaluation (v1, v2, v3) on a fresh temp DB: extraction P/R/F1 per type, planted defects, v1->v2 diff.

Usage:
  python scripts/eval_synthetic.py --mode det                 # deterministic (no LLM)
  python scripts/eval_synthetic.py --mode llm --model qwen2.5:3b [--seed-cache PATH_TO_hld.db]
  python scripts/eval_synthetic.py --render                   # eval/v3_after_parser_changes.md from the saved runs

Each run uses its own work dir eval/_work/synth_<tag>/ (db, processed JSON, traces); LLM responses are cached in that
DB (optionally seeded read-only from another DB's llm_cache). The main data/hld.db is never written.
v3 is NO LONGER held-out once the parser was adapted after reading it; this script only measures the effect.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WORK = ROOT / "eval" / "_work"
MODELS = {"qwen": "qwen2.5:3b"}


def seed_cache(dst: Path, src: Path) -> int:
    d = sqlite3.connect(str(dst))
    d.executescript("CREATE TABLE IF NOT EXISTS llm_cache(key TEXT PRIMARY KEY, model TEXT, purpose TEXT, "
                    "response TEXT, created_at TEXT)")
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    rows = s.execute("SELECT key, model, purpose, response, created_at FROM llm_cache").fetchall()
    s.close()
    d.executemany("INSERT OR IGNORE INTO llm_cache VALUES(?,?,?,?,?)", rows)
    d.commit()
    d.close()
    return len(rows)


def run(mode: str, model: str | None, seed: Path | None) -> dict:
    tag = "det" if mode == "det" else re.sub(r"[^a-z0-9]+", "_", model.lower())
    wd = WORK / f"synth_{tag}"
    wd.mkdir(parents=True, exist_ok=True)
    for f in wd.glob("*.db*"):
        f.unlink()
    os.environ["DB_PATH"] = str(wd / "t.db")
    os.environ["TRACE_PATH"] = str(wd / "traces.jsonl")
    os.environ["PROCESSED_DIR"] = str(wd / "processed")
    os.environ["CHROMA_DIR"] = str(wd / "chroma")
    os.environ["EMBEDDER"] = "hash"
    os.environ["LLM_PROVIDER"] = "ollama" if mode == "llm" else "none"
    from app import controls
    from eval import run_eval as R
    from scripts.ingest_heldout import ingest_heldout
    from scripts.ingest_synthetic import ingest_synthetic
    conn = controls.get_conn(wd / "t.db")
    n_seed = seed_cache(wd / "t.db", seed) if seed else 0
    proc, chroma = wd / "processed", wd / "chroma"
    ingest_synthetic(conn, proc, chroma, index=False)
    ingest_heldout(conn, proc, chroma, index=False)
    ids = {"syn_hld_v1", "syn_hld_v2", "syn_hld_v3_heldout"}
    if mode == "llm":
        R.extract_all(conn, model, True, ids)  # re-extract with the LLM layer (merge keeps deterministic precedence)
    syn_gt, v3_gt = ROOT / "data/synthetic/ground_truth.json", ROOT / "data/synthetic/ground_truth_v3.json"
    out = {"mode": mode, "model": model, "seeded_cache_rows": n_seed,
           "v1": R.score_synthetic(conn, syn_gt), "v3": R.score_synthetic(conn, v3_gt),
           "defects_v1": R.defects(conn, syn_gt, "1.0", "planted_defects"),
           "defects_v3": R.defects(conn, v3_gt, "3.0", "planted_defects"), "diff_v1_v2": R.diff_v1_v2(conn),
           "methods_v3": R.method_contribution(conn, v3_gt, "synthetic-ecu", "syn_hld_v3_heldout", "3.0")}
    conn.close()
    (WORK / f"synth_{tag}.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return out


def _f(x):
    return "n/a" if x is None else f"{x:.2f}"


def _cell(s: dict) -> str:
    return f"P {_f(s['precision'])} / R {_f(s['recall'])} / F1 {_f(s['f1'])}"


def render() -> str:
    runs = {t: json.loads((WORK / f"synth_{t}.json").read_text(encoding="utf-8"))
            for t in ("det", "qwen2_5_3b") if (WORK / f"synth_{t}.json").exists()}
    res = json.loads((ROOT / "eval" / "results.json").read_text(encoding="utf-8"))  # the 'before' numbers
    old = {"det": res["deterministic"]["heldout_v3"], "qwen2_5_3b": res["per_model"]["qwen2.5:3b"]["heldout_v3"]}
    old = {t: {"v3": v} for t, v in old.items()}
    cols = [("det", "deterministic"), ("qwen2_5_3b", "qwen2.5:3b")]
    L = ["# v3 before -> after the parser generalisation changes (Task 3b/3c)", "",
         "**v3 is no longer held-out**: its generator and PDF were read to diagnose the failures, so the 'after' numbers "
         "are in-sample for v3 and only show that the diagnosed failure classes are fixed (general mechanisms: fuzzy "
         "header aliases, column-role inference, prose port rules, TOC-less heading detection, LLM port/signal schema). "
         "The genuinely unseen test is the new held-out document, not v3. 'Before' = eval/results.md.", ""]
    for key, title in (("v3", "v3 extraction (before -> after)"), ("v1", "v1 extraction (after; before was 1.00 everywhere)")):
        L += [f"## {title}", "", "| entity type | " + " | ".join(n for t, n in cols if t in runs) + " |",
              "|---|" + "---|" * len(runs)]
        for typ in ("components", "ports", "interfaces", "signals", "micro_all"):
            row = []
            for t, _ in cols:
                if t not in runs:
                    continue
                after = list(runs[t][key].values())[0][typ]
                if key == "v3":
                    b = list(old[t][key].values())[0][typ]
                    row.append(f"{_cell(b)}  ->  **{_cell(after)}**")
                else:
                    row.append(_cell(after))
            L.append(f"| {typ} | " + " | ".join(row) + " |")
        L.append("")
    L += ["## Planted defects and v1->v2 diff (after)", "", "| check | " + " | ".join(n for t, n in cols if t in runs) + " |",
          "|---|" + "---|" * len(runs)]
    for key, label in (("defects_v1", "v1 defects found"), ("defects_v3", "v3 defects found")):
        L.append(f"| {label} | " + " | ".join(
            f"{len(runs[t][key]['found'])}/{runs[t][key]['n_planted']} (false positives {len(runs[t][key].get('false_positives', []))})"
            for t, _ in cols if t in runs) + " |")
    L.append("| v1->v2 diff | " + " | ".join(
        f"{runs[t]['diff_v1_v2']['tp']}/{runs[t]['diff_v1_v2']['tp'] + runs[t]['diff_v1_v2']['fn']} (fp {runs[t]['diff_v1_v2']['fp']})"
        for t, _ in cols if t in runs) + " |")
    L += ["", "## Notes", "",
          "- Root causes on v3 (before): ingest only recognised headings from page 5 on when no TOC page exists, and "
          "rejected header rows with unfamiliar words (P/R, Pattern, Producer, Consumer(s)), so tables fell into prose; "
          "empty table cells were collapsed, shifting later columns; prose ports had no deterministic rule.",
          "- With the deterministic layer now saturating v3, the LLM layer adds nothing measurable on v3 (merge keeps "
          "table precedence); its value shows on prose-only content.",
          "- Real documents (BodyComfort x2, Chassis x2, HMI): entity sets (names, per type) are byte-identical before and "
          "after the changes, both from the stored processed JSON and from re-parsing the PDFs with the new ingest.", ""]
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["det", "llm"])
    ap.add_argument("--model")
    ap.add_argument("--seed-cache")
    ap.add_argument("--render", action="store_true")
    a = ap.parse_args()
    if a.render:
        md = render()
        (ROOT / "eval" / "v3_after_parser_changes.md").write_text(md, encoding="utf-8")
        print(md)
    else:
        r = run(a.mode, a.model, Path(a.seed_cache) if a.seed_cache else None)
        print(json.dumps({k: r[k] for k in ("v1", "v3")}, indent=1, default=str)[:3000])
        print("defects_v1", r["defects_v1"]["found"], "missed", r["defects_v1"]["missed"], "FP", r["defects_v1"].get("false_positives"))
        print("defects_v3", r["defects_v3"]["found"], "missed", r["defects_v3"]["missed"])
        print("diff", {k: v for k, v in r["diff_v1_v2"].items() if k != "by_type"})
