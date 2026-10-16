"""Run the three revision comparisons and save diff JSON under data/processed/{project}/diffs/."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import controls  # noqa: E402
from app.compare import compare  # noqa: E402
from app.config import settings  # noqa: E402

PAIRS = [("body-comfort", "R3.1.5", "R3.2.3"), ("chassis", "R3.1.5", "R3.2.3"), ("synthetic-ecu", "1.0", "2.0")]


def diff_path(project_id: str, doc_a: str, doc_b: str, processed_dir: Path | None = None) -> Path:
    return (processed_dir or settings.processed_dir) / project_id / "diffs" / f"{doc_a}__{doc_b}.json"


def main() -> int:
    conn = controls.get_conn()
    for project, va, vb in PAIRS:
        ids = {}
        for v in (va, vb):
            r = conn.execute("SELECT doc_id FROM documents WHERE project_id=? AND version=?", (project, v)).fetchone()
            if r is None:
                print(f"{project}: version {v} not found")
                break
            ids[v] = r["doc_id"]
        else:
            d = compare(project, ids[va], va, ids[vb], vb, conn=conn)
            out = diff_path(project, ids[va], ids[vb])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(d.model_dump_json(indent=2), encoding="utf-8")
            print(f"== {project} {va} -> {vb}  (revisions {len(d.revisions_a)} -> {len(d.revisions_b)})")
            for t, s in d.summary.items():
                print(f"   {t:11s} {s}")
            if d.llm_only_unmatched_total:
                print(f"   (+{d.llm_only_unmatched_total} LLM-only entities on one side only, listed under llm_only_unmatched)")
            if d.sections is not None:
                print(f"   section changes {d.sections.summary}")
                for c in d.sections.modified[:5]:
                    print(f"     ~ {c.section_b} {c.title}: {c.summary}")
            print(f"   saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
