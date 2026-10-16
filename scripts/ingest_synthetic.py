"""Generate (if needed) and ingest the synthetic Window Lift HLDs into project synthetic-ecu, then extract.

Re-runnable: documents already present are skipped; extraction is idempotent.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import controls  # noqa: E402
from app.config import settings  # noqa: E402
from app.extract import extract_document  # noqa: E402
from app.ingest import ingest_document  # noqa: E402

PROJECT = "synthetic-ecu"
DOCS = [("syn_hld_v1", "hld_v1.pdf", "1.0"), ("syn_hld_v2", "hld_v2.pdf", "2.0")]


def _generator():
    spec = importlib.util.spec_from_file_location("generate_hld", settings.synthetic_dir / "generate_hld.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ingest_synthetic(conn=None, processed_dir=None, chroma_dir=None, synthetic_dir=None, index: bool = True) -> list[str]:
    syn = Path(synthetic_dir) if synthetic_dir else settings.synthetic_dir
    if not all((syn / f).exists() for _, f, _ in DOCS):
        _generator().generate(syn)
    done: list[str] = []
    with controls._use(conn) as c:
        controls.create_project(PROJECT, conn=c)
        controls.add_user("admin", "admin", conn=c)
        controls.add_member(PROJECT, "admin", conn=c)
        for doc_id, fname, version in DOCS:
            exists = c.execute("SELECT 1 FROM documents WHERE project_id=? AND doc_id=?", (PROJECT, doc_id)).fetchone()
            if exists:
                print(f"skip {doc_id} (already ingested)")
            else:
                ingest_document(syn / fname, PROJECT, "hld", version, "synthetic", "admin", doc_id=doc_id,
                                title=f"Window Lift ECU HLD v{version}", conn=c, processed_dir=processed_dir,
                                chroma_dir=chroma_dir, index=index)
                print(f"ingested {doc_id}")
            counts = extract_document(PROJECT, doc_id, conn=c, processed_dir=processed_dir)
            print(f"  extracted {counts}")
            done.append(doc_id)
    return done


if __name__ == "__main__":
    ingest_synthetic()
