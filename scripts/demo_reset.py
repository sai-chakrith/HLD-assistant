"""Rebuild a clean demo state from scratch: seed, ingest real + synthetic + held-out, extract, checks, diffs, reports.

Usage: python scripts/demo_reset.py --yes [--data-dir D] [--db-path P] [--chroma-dir C] [--processed-dir R]
           [--raw-dir W] [--keep-llm-cache] [--skip-reports] [--model M]
Targets also come from env (DATA_DIR, DB_PATH, CHROMA_DIR, PROCESSED_DIR, RAW_DIR). Nothing is deleted without --yes, and the
main checkout's data/hld.db is refused without --yes (it holds the LLM cache; --keep-llm-cache copies llm_cache over).
Each step runs the existing script in a subprocess with the target paths in its environment. Prints per-step timings.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_DB = ROOT / "data" / "hld.db"
REPORT_DOCS = [("body-comfort", "bodycomfort_r3.2.3", "Wiper"), ("synthetic-ecu", "syn_hld_v1", "WinCtrlManager")]


class Refused(SystemExit):
    pass


def resolve_target(args: argparse.Namespace, env: dict | None = None) -> dict[str, Path]:
    e = env if env is not None else os.environ
    data = Path(args.data_dir or e.get("DATA_DIR") or ROOT / "data").resolve()

    def pick(a, k, d):
        return Path(a or e.get(k) or d).resolve()

    return {"DATA_DIR": data, "DB_PATH": pick(args.db_path, "DB_PATH", data / "hld.db"),
            "CHROMA_DIR": pick(args.chroma_dir, "CHROMA_DIR", ROOT / "chroma"),
            "PROCESSED_DIR": pick(args.processed_dir, "PROCESSED_DIR", data / "processed"),
            "RAW_DIR": pick(args.raw_dir, "RAW_DIR", data / "raw")}


def check_sources(t: dict[str, Path]) -> None:
    """Stop before wiping anything when there is nothing to ingest: the real PDFs are read from RAW_DIR."""
    if not any(Path(t["RAW_DIR"]).glob("*.pdf")):
        raise Refused(f"no PDF in RAW_DIR {t['RAW_DIR']}: pass --raw-dir (e.g. --raw-dir data/raw) so the real "
                      "documents can be ingested")


def check_safe(t: dict[str, Path], yes: bool) -> None:
    """Refuse to delete anything without --yes (the main checkout's hld.db is named explicitly)."""
    if not yes:
        extra = " (the main checkout's data/hld.db, it holds the LLM cache)" if t["DB_PATH"] == MAIN_DB.resolve() else ""
        raise Refused(f"refusing to delete {t['DB_PATH']}{extra}, {t['CHROMA_DIR']}, {t['PROCESSED_DIR']} without --yes")


def wipe(t: dict[str, Path], keep_cache: bool) -> int:
    """Delete the target db/chroma/processed; optionally re-insert the llm_cache rows. Returns rows kept."""
    rows: list = []
    db = t["DB_PATH"]
    if keep_cache and db.exists():
        c = sqlite3.connect(db)
        try:
            rows = c.execute("SELECT key,model,purpose,response,created_at FROM llm_cache").fetchall()
        except sqlite3.Error:
            rows = []
        c.close()
    for d in (t["CHROMA_DIR"], t["PROCESSED_DIR"]):
        shutil.rmtree(d, ignore_errors=True)
    for f in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        f.unlink(missing_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE IF NOT EXISTS llm_cache(key TEXT PRIMARY KEY, model TEXT, purpose TEXT, "
                  "response TEXT, created_at TEXT)")
        c.executemany("INSERT OR REPLACE INTO llm_cache VALUES(?,?,?,?,?)", rows)
        c.commit()
        c.close()
    return len(rows)


def steps(model: str | None, skip_reports: bool) -> list[tuple[str, list[str]]]:
    py = sys.executable
    s = [("seed", [py, "scripts/seed.py"]), ("ingest real", [py, "scripts/ingest_all.py"]),
         ("ingest synthetic", [py, "scripts/ingest_synthetic.py"]), ("ingest held-out", [py, "scripts/ingest_heldout.py"]),
         ("extract", [py, "scripts/extract_all.py"]), ("checks", [py, "scripts/run_checks_all.py"]),
         ("diffs", [py, "scripts/compare_all.py"])]
    if not skip_reports:
        for proj, doc, comp in REPORT_DOCS:
            cmd = [py, "scripts/run_reports.py", proj, doc, "--component", comp, "--doc-summary"]
            s.append((f"report {doc}", cmd + (["--model", model] if model else [])))
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true", help="required to delete existing target data")
    for n in ("data-dir", "db-path", "chroma-dir", "processed-dir", "raw-dir"):
        ap.add_argument("--" + n)
    ap.add_argument("--keep-llm-cache", action="store_true")
    ap.add_argument("--skip-reports", action="store_true")
    ap.add_argument("--model", default=os.environ.get("DEMO_MODEL", "qwen3:4b-instruct"))
    a = ap.parse_args(argv)
    t = resolve_target(a)
    check_sources(t)
    check_safe(t, a.yes)
    print("target:", {k: str(v) for k, v in t.items()})
    t0 = time.perf_counter()
    kept = wipe(t, a.keep_llm_cache)
    print(f"wiped target (kept {kept} llm_cache rows)")
    env = {**os.environ, **{k: str(v) for k, v in t.items()}, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8",
           "SYNTHETIC_DIR": os.environ.get("SYNTHETIC_DIR", str(ROOT / "data" / "synthetic")),
           "TRACE_PATH": str(t["DATA_DIR"] / "traces.jsonl")}
    timings: list[tuple[str, float, int]] = []
    for name, cmd in steps(a.model, a.skip_reports):
        s0 = time.perf_counter()
        rc = subprocess.run(cmd, cwd=ROOT, env=env).returncode
        timings.append((name, time.perf_counter() - s0, rc))
        print(f"== {name}: {timings[-1][1]:.1f}s rc={rc}", flush=True)
    total = time.perf_counter() - t0
    print("\nstep timings:")
    for n, sec, rc in timings:
        print(f"  {n:28} {sec:8.1f}s {'' if rc == 0 else 'FAILED rc=' + str(rc)}")
    print(f"  {'TOTAL':28} {total:8.1f}s")
    return 1 if any(rc for _, _, rc in timings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
