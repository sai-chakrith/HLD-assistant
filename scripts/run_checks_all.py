"""Run the rule-based checks on every ingested document and print finding counts per rule."""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import controls  # noqa: E402
from app.checks import run_checks  # noqa: E402

RULES = ["M1", "M2", "M3", "M4", "C1", "C2", "C3", "C4"]


def main() -> None:
    conn = controls.get_conn()
    docs = conn.execute("SELECT project_id, doc_id, version, source_status FROM documents "
                        "ORDER BY project_id, doc_id").fetchall()
    print(f"{'doc_id':24} {'status':10} " + " ".join(f"{r:>3}" for r in RULES) + "  total")
    for d in docs:
        try:
            fs = run_checks(d["project_id"], d["doc_id"], d["version"], conn=conn)
        except FileNotFoundError:
            print(f"{d['doc_id']:24} processed JSON missing, skipped")
            continue
        cnt = Counter(f.rule_id for f in fs)
        print(f"{d['doc_id']:24} {d['source_status']:10} " + " ".join(f"{cnt.get(r, 0):>3}" for r in RULES)
              + f"  {len(fs):>5}")


if __name__ == "__main__":
    main()
