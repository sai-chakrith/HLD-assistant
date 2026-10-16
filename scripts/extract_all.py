"""Run extraction for every document in the DB, print counts, build + export graphs.

Usage: python scripts/extract_all.py [--llm] [--model qwen3:4b-instruct] [--doc DOC_ID ...] [--no-diagram] [--no-convention]
Layers: table + heuristics always; diagram geometry (PDF located by sha256) and naming convention by default;
--llm adds prose extraction with the local Ollama model (cached; slow the first time).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import controls  # noqa: E402
from app.extract import extract_document, method_counts  # noqa: E402
from app.graph import build_graph, export_json, export_pyvis, graph_dir  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--doc", nargs="*", default=None)
    ap.add_argument("--no-diagram", action="store_true")
    ap.add_argument("--no-convention", action="store_true")
    a = ap.parse_args()
    conn = controls.get_conn()
    docs = conn.execute("SELECT project_id, doc_id, version FROM documents ORDER BY project_id, doc_id").fetchall()
    print(f"{'doc_id':24} {'comp':>5} {'port':>5} {'intf':>5} {'sig':>5}  graph(nodes/edges)  seconds")
    for d in docs:
        if a.doc and d["doc_id"] not in a.doc:
            continue
        t0 = time.perf_counter()
        stats: dict = {}
        try:
            c = extract_document(d["project_id"], d["doc_id"], conn=conn, use_llm=a.llm, model=a.model,
                                 use_diagram=not a.no_diagram, use_convention=not a.no_convention, stats=stats)
        except FileNotFoundError:
            print(f"{d['doc_id']:24} processed JSON missing, skipped")
            continue
        g = build_graph(d["project_id"], d["doc_id"], d["version"], conn)
        gd = graph_dir(d["project_id"])
        export_json(g, gd / f"{d['doc_id']}.json")
        export_pyvis(g, gd / f"{d['doc_id']}.html")
        print(f"{d['doc_id']:24} {c['components']:5} {c['ports']:5} {c['interfaces']:5} {c['signals']:5}  "
              f"{g.number_of_nodes()}/{g.number_of_edges()}  {time.perf_counter() - t0:.1f}s  llm={stats or '-'}")
        print("   methods:", {t: m for t, m in method_counts(d["project_id"], d["doc_id"], conn).items() if m})


if __name__ == "__main__":
    main()
