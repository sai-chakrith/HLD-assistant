"""Generate (if needed) and ingest a HELD-OUT synthetic HLD (v3 or v4) into project synthetic-ecu.

Usage: python scripts/ingest_heldout.py [--doc v3|v4] [--llm] [--model M] [--quiet]
v3 was used to develop the Task 3b parser changes, so it is no longer held-out. v4 (eval/heldout_v4.lock) was frozen
before those changes; --quiet suppresses the extraction counts so the v4 output is first seen in eval/run_eval.py.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import controls  # noqa: E402
from app.config import settings  # noqa: E402
from app.extract import extract_document  # noqa: E402
from app.ingest import ingest_document  # noqa: E402

PROJECT = "synthetic-ecu"
HELDOUT = {  # key: (doc_id, file, version, generator, title)
    "v3": ("syn_hld_v3_heldout", "hld_v3_heldout.pdf", "3.0", "generate_heldout.py", "Seat Comfort ECU SAD v3 (held-out)"),
    "v4": ("syn_hld_v4_heldout", "hld_v4_heldout.pdf", "4.0", "generate_heldout_v4.py", "Door Control ECU v4 (held-out)"),
}
DOC_ID, FILE, VERSION = HELDOUT["v3"][:3]  # backwards-compatible names


def _generator(gen_file: str = "generate_heldout.py"):
    spec = importlib.util.spec_from_file_location(gen_file[:-3], settings.synthetic_dir / gen_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ingest_heldout(conn=None, processed_dir=None, chroma_dir=None, synthetic_dir=None, index: bool = True,
                   use_llm: bool = False, model: str | None = None, doc: str = "v3", quiet: bool = False) -> str:
    doc_id, fname, version, gen_file, title = HELDOUT[doc]
    syn = Path(synthetic_dir) if synthetic_dir else settings.synthetic_dir
    if not (syn / fname).exists():
        if doc == "v4":  # frozen document: never regenerate silently (the lock records its sha256)
            raise FileNotFoundError(f"{syn / fname} missing; restore it from git (see eval/heldout_v4.lock)")
        _generator(gen_file).generate(syn)
    with controls._use(conn) as c:
        controls.create_project(PROJECT, conn=c)
        controls.add_user("admin", "admin", conn=c)
        controls.add_member(PROJECT, "admin", conn=c)
        if not c.execute("SELECT 1 FROM documents WHERE project_id=? AND doc_id=?", (PROJECT, doc_id)).fetchone():
            ingest_document(syn / fname, PROJECT, "hld", version, "synthetic", "admin", doc_id=doc_id,
                            title=title, conn=c, processed_dir=processed_dir, chroma_dir=chroma_dir, index=index)
            print(f"ingested {doc_id}")
        counts = extract_document(PROJECT, doc_id, conn=c, processed_dir=processed_dir, use_llm=use_llm, model=model)
        print("extracted", "(counts hidden: --quiet)" if quiet else counts)
    return doc_id


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--doc", choices=sorted(HELDOUT), default="v3")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    ingest_heldout(use_llm=a.llm, model=a.model, doc=a.doc, quiet=a.quiet)
