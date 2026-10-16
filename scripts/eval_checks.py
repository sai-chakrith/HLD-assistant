"""Compare rule findings on the synthetic docs with the planted defects in ground_truth.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import controls  # noqa: E402
from app.checks import run_checks  # noqa: E402
from app.config import settings  # noqa: E402
from app.extract import normalize_name  # noqa: E402


def _sig(rule: str, entities: list[str]) -> tuple:
    return rule, frozenset(normalize_name(e) for e in entities)


def evaluate(findings, expected: list[dict]) -> dict:
    exp = {_sig(d["rule_id"], d["entities"]): d for d in expected}
    got = {_sig(f.rule_id, f.entities): f for f in findings}
    return {"found": [exp[k]["rule_id"] for k in exp if k in got],
            "missed": [exp[k]["rule_id"] for k in exp if k not in got],
            "false_positives": [f"{got[k].rule_id}:{'/'.join(got[k].entities)}" for k in got if k not in exp]}


def main() -> int:
    gt = json.loads((settings.synthetic_dir / "ground_truth.json").read_text(encoding="utf-8"))
    conn = controls.get_conn()
    for ver, key in (("1.0", "planted_defects"), ("2.0", "remaining_defects_v2")):
        d = gt["documents"][ver]
        fs = run_checks(gt["project_id"], d["doc_id"], ver, conn=conn)
        r = evaluate(fs, [x for x in gt[key] if x["version"] == ver])
        total = len(r["found"]) + len(r["missed"])
        print(f"== {d['doc_id']} v{ver}: found {len(r['found'])}/{total}  missed {r['missed']}  "
              f"false positives {len(r['false_positives'])} {r['false_positives']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
