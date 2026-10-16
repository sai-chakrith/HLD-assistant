"""Ingest data/synthetic/scanned_sample.pdf (OCR) into project synthetic-ecu as syn_scanned_sample and run one query.

Usage: python scripts/ingest_scanned.py [query]
If the doc_id already exists it is reported and left untouched (nothing is deleted).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import controls, retrieve  # noqa: E402
from app.config import settings  # noqa: E402
from app.ingest import ingest_document, load_processed  # noqa: E402

PROJECT, DOC_ID, FILE, VERSION = "synthetic-ecu", "syn_scanned_sample", "scanned_sample.pdf", "1.0-scan"
QUERY = "Which component provides the ISwitchCmd interface and what are the steps of manual window operation?"


def ingest_scanned(conn=None, processed_dir=None, chroma_dir=None, synthetic_dir=None, index: bool = True) -> str:
    syn = Path(synthetic_dir) if synthetic_dir else settings.synthetic_dir
    with controls._use(conn) as c:
        controls.create_project(PROJECT, conn=c)
        controls.add_user("admin", "admin", conn=c)
        controls.add_member(PROJECT, "admin", conn=c)
        if c.execute("SELECT 1 FROM documents WHERE project_id=? AND doc_id=?", (PROJECT, DOC_ID)).fetchone():
            print(f"{DOC_ID} already exists in {PROJECT}; left untouched")
            return "exists"
        ingest_document(syn / FILE, PROJECT, "hld", VERSION, "synthetic", "admin", doc_id=DOC_ID,
                        title="Window Lift ECU HLD (scanned sample)", conn=c, processed_dir=processed_dir,
                        chroma_dir=chroma_dir, index=index)
    print(f"ingested {DOC_ID}")
    return "ingested"


if __name__ == "__main__":
    ingest_scanned()
    try:
        print("ocr_pages:", load_processed(PROJECT, DOC_ID).get("ocr_pages"))
    except FileNotFoundError:
        pass
    q = " ".join(sys.argv[1:]) or QUERY
    print("query:", q)
    for h in retrieve.search(PROJECT, q, k=8):
        m = h["metadata"]
        if m["doc_id"] != DOC_ID:
            continue
        print(f"  {m['doc_id']} p.{m['page_start']} sec {m['section_id']} ocr={m.get('ocr')} "
              f"conf={m.get('ocr_confidence')} score={h['score']:.3f} | {h['text'][:90]!r}")
