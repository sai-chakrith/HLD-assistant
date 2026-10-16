"""Remove one document's derived data (DB rows, Chroma chunks, processed JSON) so it can be ingested again with
the current parser. The DB is backed up first; the LLM cache, users, reviews and audit log are never touched.

Usage: python scripts/reingest_doc.py <project_id> <doc_id> --yes
Then re-ingest, e.g. python scripts/ingest_heldout.py --doc v3
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import controls, retrieve  # noqa: E402
from app.config import settings  # noqa: E402
from app.ingest import processed_path  # noqa: E402

DOC_TABLES = ("components", "ports", "interfaces", "signals", "findings", "reports", "documents")


def remove_document(project_id: str, doc_id: str, conn=None, processed_dir=None, chroma_dir=None) -> dict[str, int]:
    """Delete the document's rows from DOC_TABLES, its chunks and its processed JSON. Returns rows deleted per table."""
    out: dict[str, int] = {}
    with controls._use(conn) as c:
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in DOC_TABLES:
            if t in have:
                out[t] = c.execute(f"DELETE FROM {t} WHERE project_id=? AND doc_id=?", (project_id, doc_id)).rowcount
        controls.log_action("admin", "document_removed_for_reingest", f"{project_id}:{doc_id}", out, c)
        c.commit()
    retrieve.delete_document(project_id, doc_id, chroma_dir=chroma_dir)
    processed_path(project_id, doc_id, processed_dir).unlink(missing_ok=True)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id")
    ap.add_argument("doc_id")
    ap.add_argument("--yes", action="store_true", help="required: confirms deletion")
    a = ap.parse_args()
    if not a.yes:
        sys.exit("refusing without --yes")
    backup = settings.db_path.with_name(f"{settings.db_path.stem}.backup-{time.strftime('%Y%m%d-%H%M%S')}.db")
    shutil.copy(settings.db_path, backup)
    print("backup", backup)
    print("removed", remove_document(a.project_id, a.doc_id))
