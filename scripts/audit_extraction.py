"""Reproducible development error audit. Does not tune, modify gold, or run held-out documents."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path


def annotation_matches(kind, row, entity, name_match):
    if not name_match(row["name"], entity["name"]):
        return False
    if kind == "port":
        return ((not row.get("component") or name_match(row["component"], entity.get("component", "")))
                and (not row.get("direction") or row["direction"] == entity.get("direction")))
    if kind == "signal" and row.get("component"):
        senders = [entity.get("sender", ""), *(entity.get("extra_senders") or [])]
        receivers = entity.get("receivers") or []
        endpoints = senders if row.get("direction") == "provided" else receivers if row.get("direction") == "required" else senders + receivers
        return any(name_match(row["component"], endpoint) for endpoint in endpoints)
    return True


def field_aware_score(gold, inventories, name_match):
    """One candidate must match every annotated field; names alone do not establish a correct port."""
    scores = {}
    for kind, table in {"component": "components", "port": "ports", "interface": "interfaces", "signal": "signals"}.items():
        rows = [r for r in gold if r["type"] == kind]
        if not rows:
            continue
        matched = 0
        for row in rows:
            candidates = inventories.get(row["doc_id"], {}).get(table, [])
            matched += any(annotation_matches(kind, row, e, name_match) for e in candidates)
        scores[kind] = {"annotated": len(rows), "joint_matches": matched, "joint_recall": matched / len(rows)}
    return scores


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root))
    from app import extract, ingest
    from eval.run_eval import name_match, score_real_gold
    gold_path = root / "eval/real_gold.csv"
    with gold_path.open(encoding="utf-8-sig", newline="") as stream:
        gold = [r for r in csv.DictReader(stream) if r.get("verified", "").lower() == "yes"]
    inventories, missing, documents = {}, [], []
    for doc_id in sorted({r["doc_id"] for r in gold}):
        pdf = (args.raw_dir or root / "data/raw") / f"{doc_id}.pdf"
        if not pdf.exists():
            missing.append(doc_id)
            continue
        doc = ingest.parse_pdf(pdf, doc_id)
        entities = extract.extract_entities(doc, pdf_path=pdf)
        inv = {}
        for table, rows in entities.items():
            inv[table] = [{**e.model_dump(), **e.field_dict(), "page": e.sources[0].page if e.sources else None}
                          for e in rows if not extract.is_junk_name(e.name)]
        inventories[doc_id] = inv
        documents.append({"doc_id": doc_id, "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                          "inventory": inv})
    errors = []
    for row in gold:
        if row["doc_id"] not in inventories:
            continue
        table = extract.ENTITY_TABLES[row["type"]]
        matches = [e for e in inventories[row["doc_id"]][table] if name_match(row["name"], e["name"])]
        reason = "missing" if not matches else ""
        if matches and not any(annotation_matches(row["type"], row, e, name_match) for e in matches):
            reason = "owner_or_direction_mismatch" if table == "ports" else "endpoint_mismatch"
        if reason:
            errors.append({"gold": row, "reason": reason, "candidates": matches})
    report = {"scope": "development gold; deterministic extraction only; not independent acceptance",
              "gold_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
              "missing_documents": missing, "metrics": score_real_gold(gold, inventories) if not missing else None,
              "field_aware": field_aware_score(gold, inventories, name_match) if not missing else None,
              "errors": errors, "documents": documents}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "missing_documents": missing, "errors": len(errors)}))
    return int(bool(missing))


if __name__ == "__main__":
    raise SystemExit(main())
