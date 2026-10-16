"""Generate real LLM reports: component report + document summary (+ flow summary) for one doc.
Usage: PYTHONPATH=. python scripts/run_reports.py <project> <doc_id> [--component NAME] [--doc-summary] [--flow]"""
from __future__ import annotations

import argparse
import json

from app import controls, summarize
from app.ingest import load_processed

ap = argparse.ArgumentParser()
ap.add_argument("project")
ap.add_argument("doc_id")
ap.add_argument("--component")
ap.add_argument("--doc-summary", action="store_true")
ap.add_argument("--flow", action="store_true")
ap.add_argument("--model")
a = ap.parse_args()
c = controls.get_conn()
row = c.execute("SELECT version FROM documents WHERE project_id=? AND doc_id=?", (a.project, a.doc_id)).fetchone()
if row is None:
    raise SystemExit(f"document {a.doc_id!r} is not ingested in project {a.project!r}")
ver = row[0]
if a.component:
    r = summarize.component_report(a.project, a.doc_id, ver, a.component, conn=c, model=a.model)
    print(json.dumps({k: r[k] for k in ("component", "description", "purpose", "purpose_citations", "confidence",
                                        "limitation", "model")}, indent=1, ensure_ascii=False))
if a.doc_summary:
    r = summarize.document_summary(a.project, a.doc_id, ver, conn=c, model=a.model)
    print(json.dumps({k: r[k] for k in ("summary", "confidence", "limitation", "model")}, indent=1, ensure_ascii=False))
    print("bullets", len(r["bullets"]), json.dumps(r["bullets"][:4], ensure_ascii=False, indent=1))
    print("citations", json.dumps(r["citations"], ensure_ascii=False)[:1500])
if a.flow:
    r = summarize.functional_flow_summary(a.project, a.doc_id, ver, conn=c, processed_dir=None)
    print(r["source"], r["confidence"], r["limitation"], "flagged", r["flagged_steps"])
    for f in r["flows"][:2]:
        print(f["title"])
        for s in f["steps"][:4]:
            print("  ", s["n"], s["text"], s["citation"], s["unknown_entities"])
