"""Check the synthetic v1->v2 diff against ground_truth.json expected_diff."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import controls  # noqa: E402
from app.compare import ENTITY_TYPES, Diff, compare  # noqa: E402
from app.config import settings  # noqa: E402
from app.extract import normalize_name  # noqa: E402


def _nk(k: str) -> str:
    return ".".join(normalize_name(p) for p in k.split("."))


def evaluate(diff: Diff, expected: dict) -> dict:
    """Return {type: {added|removed|changed: (tp, fn, fp)}} and overall accuracy."""
    res, tp_all, fn_all, fp_all = {}, 0, 0, 0
    for t in ENTITY_TYPES:
        ex, td = expected.get(t, {}), getattr(diff, t)
        res[t] = {}
        for kind in ("added", "removed"):
            e = {_nk(k) for k in ex.get(kind, [])}
            g = {x.key for x in getattr(td, kind)}
            res[t][kind] = (len(e & g), len(e - g), len(g - e))
        e = {_nk(k): frozenset(v) for k, v in ex.get("changed", {}).items()}
        g = {x.key: frozenset(x.changes) for x in td.changed}
        tp = sum(1 for k in e if g.get(k) == e[k])
        res[t]["changed"] = (tp, len(e) - tp, sum(1 for k in g if e.get(k) != g[k]))
        for tpf in res[t].values():
            tp_all, fn_all, fp_all = tp_all + tpf[0], fn_all + tpf[1], fp_all + tpf[2]
    total = tp_all + fn_all + fp_all
    return {"by_type": res, "tp": tp_all, "fn": fn_all, "fp": fp_all,
            "accuracy": tp_all / total if total else 1.0}


def main() -> int:
    gt = json.loads((settings.synthetic_dir / "ground_truth.json").read_text(encoding="utf-8"))
    ed = gt["expected_diff"]
    da, db = gt["documents"][ed["from"]]["doc_id"], gt["documents"][ed["to"]]["doc_id"]
    d = compare(gt["project_id"], da, ed["from"], db, ed["to"], conn=controls.get_conn())
    r = evaluate(d, ed)
    for t, kinds in r["by_type"].items():
        print(f"{t:11s} " + "  ".join(f"{k}: TP={v[0]} FN={v[1]} FP={v[2]}" for k, v in kinds.items()))
    print(f"TP={r['tp']} FN={r['fn']} FP={r['fp']}  accuracy={r['accuracy']:.1%}  (target >= 90%)")
    return 0 if r["accuracy"] >= 0.9 else 1


if __name__ == "__main__":
    raise SystemExit(main())
