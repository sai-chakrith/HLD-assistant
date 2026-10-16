"""Ingest every PDF in data/raw/manifest.json into its project (as admin)."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import controls  # noqa: E402
from app.config import settings  # noqa: E402
from app.ingest import IngestRejected, ingest_document, load_processed  # noqa: E402
from app.retrieve import get_embedder  # noqa: E402
from app.chunk import chunk_document  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
for _n in ("httpx", "httpcore", "huggingface_hub", "sentence_transformers"):
    logging.getLogger(_n).setLevel(logging.WARNING)


def main() -> None:
    from scripts.seed import seed
    conn = controls.get_conn()
    seed(conn)
    print(f"Embedder: {get_embedder().name}")
    manifest = json.loads((settings.raw_dir / "manifest.json").read_text(encoding="utf-8"))
    for m in manifest:
        try:
            doc_id = ingest_document(
                settings.raw_dir / m["filename"], m["project_id"], "interface_spec", m["release"],
                m.get("source_status", "approved"), "admin", doc_id=m["doc_id"], title=m["title"], conn=conn)
        except IngestRejected as e:
            print(f"SKIP {m['doc_id']}: {e}")
            continue
        doc = load_processed(m["project_id"], doc_id)
        tables = sum(len(s["tables"]) for s in doc["sections"])
        print(f"{doc_id}: sections={len(doc['sections'])} tables={tables} "
              f"chunks={len(chunk_document(doc))} revisions={len(doc['revisions'])}")


if __name__ == "__main__":
    main()
