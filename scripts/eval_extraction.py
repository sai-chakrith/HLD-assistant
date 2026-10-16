"""Compare stored extraction of the synthetic docs against data/synthetic/ground_truth.json (precision/recall)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import controls  # noqa: E402
from app.config import settings  # noqa: E402
from app.extract import get_inventory, normalize_name  # noqa: E402

KIND = {"sender_receiver": "sender_receiver", "client_server": "client_server", "": "unknown"}


def _key(t: str, e: dict) -> str:
    if t == "ports":
        return normalize_name(e["component"]) + "." + normalize_name(e["name"])
    return normalize_name(e["name"])


def _fields(t: str, e: dict) -> tuple:
    if t == "components":
        return (e.get("description", ""), e.get("category", ""))
    if t == "ports":
        return (e.get("direction"), normalize_name(e.get("interface", "")), KIND.get(e.get("kind", ""), e.get("kind")))
    if t == "interfaces":
        return (KIND.get(e.get("kind", ""), e.get("kind")), tuple(e.get("data_elements", [])))
    return (normalize_name(e.get("sender", "")), tuple(normalize_name(r) for r in e.get("receivers", [])),
            e.get("data_type", ""))


def score(gt_entities: dict, inv: dict) -> dict:
    out = {}
    for t, gt_list in gt_entities.items():
        gt = {_key(t, e): _fields(t, e) for e in gt_list}
        ex = {_key(t, e): _fields(t, e) for e in inv.get(t, [])}
        tp = gt.keys() & ex.keys()
        p = len(tp) / len(ex) if ex else 1.0
        r = len(tp) / len(gt) if gt else 1.0
        fexact = sum(1 for k in tp if gt[k] == ex[k]) / len(tp) if tp else 0.0
        out[t] = {"precision": p, "recall": r, "field_match": fexact, "gt": len(gt), "extracted": len(ex),
                  "missing": sorted(gt.keys() - ex.keys()), "extra": sorted(ex.keys() - gt.keys())}
    return out


def main() -> int:
    gt = json.loads((settings.synthetic_dir / "ground_truth.json").read_text(encoding="utf-8"))
    conn = controls.get_conn()
    worst = 1.0
    for ver, d in gt["documents"].items():
        inv = get_inventory(gt["project_id"], d["doc_id"], ver, conn)
        print(f"== {d['doc_id']} (v{ver})")
        for t, s in score(gt["entities"][ver], inv).items():
            print(f"  {t:11} P={s['precision']:.2f} R={s['recall']:.2f} fields={s['field_match']:.2f} "
                  f"({s['extracted']} extracted / {s['gt']} truth) missing={s['missing']} extra={s['extra']}")
            worst = min(worst, s["precision"], s["recall"])
    print(f"worst P/R: {worst:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
