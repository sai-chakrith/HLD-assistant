"""Export inventory, findings, diffs and reports to CSV / Excel / JSON.

Every row carries review_status + citation columns (doc_id, version, page, section).
Excel: one sheet per table. If any row is unreviewed, row 1 of EVERY sheet is the note
"Contains unreviewed AI output" and the table header is on row 2 (otherwise the header is row 1).
JSON: {"note": ..., "tables": {name: [rows]}}.  CSV: one file per table, header on row 1, no note row
(review_status column carries the flag).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from app import controls
from app.compare import ENTITY_TYPES, Diff
from app.config import settings
from app.extract import get_inventory

NOTE = "Contains unreviewed AI output"
CITE_COLS = ["doc_id", "version", "page", "section"]
TAIL_COLS = ["review_status"] + CITE_COLS
FORMATS = {"csv", "xlsx", "json"}
INV_FIELDS = {
    "components": ["name", "description", "category"],
    "ports": ["name", "component", "direction", "interface", "kind", "description"],
    "interfaces": ["name", "kind", "data_elements", "description"],
    "signals": ["name", "sender", "receivers", "data_type", "description"],
}


def default_out_dir(project_id: str) -> Path:
    return settings.processed_dir / project_id / "exports"


def _cell(v: Any) -> Any:
    if isinstance(v, (list, tuple, set)):
        return "; ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, default=str)
    return v


def _tidy(rows: list[dict], columns: list[str] | None) -> tuple[list[str], list[dict]]:
    cols = list(columns or [])
    for r in rows:
        for k in r:
            if k not in cols and k not in TAIL_COLS:
                cols.append(k)
    cols = [c for c in cols if c not in TAIL_COLS] + TAIL_COLS
    out = []
    for r in rows:
        r = dict(r)
        r.setdefault("review_status", "unreviewed")
        out.append({c: _cell(r.get(c)) for c in cols})
    return cols, out


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:60] or "export"


def write_tables(tables: dict[str, list[dict]], basename: str, fmt: str, out_dir: Path | str,
                 columns: dict[str, list[str]] | None = None) -> list[Path]:
    """Generic rows-based exporter: {table_name: [row dicts]} -> files. Reusable for component reports."""
    fmt = fmt.lower().lstrip(".")
    if fmt == "excel":
        fmt = "xlsx"
    if fmt not in FORMATS:
        raise ValueError(f"unsupported format {fmt!r}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    columns = columns or {}
    tidy = {n: _tidy(rows, columns.get(n)) for n, rows in tables.items()}
    unreviewed = any(r.get("review_status") != "approved" for _, rows in tidy.values() for r in rows)
    base = _safe(basename)
    if fmt == "csv":
        paths = []
        for n, (cols, rows) in tidy.items():
            p = out / f"{base}_{_safe(n)}.csv"
            pd.DataFrame(rows, columns=cols).to_csv(p, index=False, encoding="utf-8-sig")
            paths.append(p)
        return paths
    if fmt == "json":
        p = out / f"{base}.json"
        payload = {"note": NOTE if unreviewed else "", "tables": {n: rows for n, (_, rows) in tidy.items()}}
        p.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        return [p]
    p = out / f"{base}.xlsx"
    with pd.ExcelWriter(p, engine="openpyxl") as xw:
        for n, (cols, rows) in tidy.items():
            sheet = _safe(n)[:31]
            pd.DataFrame(rows, columns=cols).to_excel(xw, sheet_name=sheet, index=False,
                                                      startrow=1 if unreviewed else 0)
            if unreviewed:
                xw.sheets[sheet].cell(row=1, column=1, value=NOTE)
        if not tidy:
            pd.DataFrame().to_excel(xw, sheet_name="empty")
    return [p]


# ------------------------------------------------------------------ builders

def inventory_tables(project_id: str, doc_id: str | None = None, version: str | None = None,
                     conn=None) -> dict[str, list[dict]]:
    inv = get_inventory(project_id, doc_id, version, conn=conn)
    return {t: [{**{f: r.get(f) for f in INV_FIELDS[t]}, "extraction_method": r.get("extraction_method"),
                 "review_status": r["review_status"], "doc_id": r["doc_id"], "version": r["version"],
                 "page": r["page"], "section": r["section"]} for r in inv.get(t, [])] for t in ENTITY_TYPES}


def findings_rows(project_id: str, doc_id: str | None = None, version: str | None = None,
                  conn=None) -> list[dict]:
    from app.checks import get_findings
    rows = []
    for f in get_findings(project_id, doc_id=doc_id, version=version, conn=conn):
        base = {"id": f.id, "rule_id": f.rule_id, "severity": f.severity, "title": f.title,
                "explanation": f.explanation, "entities": f.entities, "label": f.label,
                "review_status": f.review_status}
        for ev in f.evidence or [None]:
            rows.append({**base, "doc_id": ev.doc_id if ev else f.doc_id, "version": ev.version if ev else f.version,
                         "page": ev.page if ev else None, "section": ev.section if ev else ""})
    return rows


def diff_tables(diff: Diff) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {}
    for t in ENTITY_TYPES:
        td, rows = getattr(diff, t), []
        for kind in ("added", "removed", "changed"):
            for e in getattr(td, kind):
                # added -> citation in B; removed -> in A; changed -> B (last citation)
                c = e.citations[-1] if e.citations else None
                base = {"change": kind, "key": e.key, "name": e.name,
                        "doc_id": c.doc_id if c else None, "version": c.version if c else None,
                        "page": c.page if c else None, "section": c.section if c else "",
                        "review_status": diff.review_status}
                if kind == "changed":
                    for fld, ch in e.changes.items():
                        rows.append({**base, "field": fld, "old": ch["old"], "new": ch["new"]})
                else:
                    rows.append({**base, "field": "", "old": "", "new": ""})
        for rn in td.possible_renames:
            rows.append({"change": "possible_rename", "key": "", "name": f"{rn.old} -> {rn.new}",
                         "field": "score", "old": "", "new": rn.score, "doc_id": diff.doc_b,
                         "version": diff.version_b, "page": None, "section": "",
                         "review_status": diff.review_status})
        tables[t] = rows
    rev = []
    for side, doc, ver, revs in (("A", diff.doc_a, diff.version_a, diff.revisions_a),
                                 ("B", diff.doc_b, diff.version_b, diff.revisions_b)):
        for r in revs:
            rev.append({"side": side, "date": r.get("date"), "rev_version": r.get("version"),
                        "changed_by": r.get("changed_by"), "description": r.get("description"),
                        "doc_id": doc, "version": ver, "page": None, "section": "change history",
                        "review_status": diff.review_status})
    tables["revisions"] = rev
    if diff.sections is not None:
        sec = []
        for bucket in ("added", "removed", "modified", "front_matter"):
            for c in getattr(diff.sections, bucket):
                sec.append({"bucket": bucket, "change": c.change, "section_a": c.section_a, "section_b": c.section_b,
                            "title": c.title, "pages_a": "-".join(str(p) for p in c.pages_a if p is not None),
                            "pages_b": "-".join(str(p) for p in c.pages_b if p is not None),
                            "words_added": c.words_added, "words_removed": c.words_removed, "summary": c.summary,
                            "changed_text": " || ".join(f"[{s['op']}] {s['old']} => {s['new']}" for s in c.sentences),
                            "rows_added": " || ".join(c.rows_added), "rows_removed": " || ".join(c.rows_removed),
                            "doc_id": diff.doc_b, "version": diff.version_b,
                            "page": c.pages_b[0] if c.pages_b else (c.pages_a[0] if c.pages_a else None),
                            "section": c.section_b or c.section_a or "", "review_status": diff.review_status})
        tables["section_changes"] = sec
    return tables


# ------------------------------------------------------------------ public API

def export_inventory(project_id: str, fmt: str, out_dir: Path | str | None = None, doc_id: str | None = None,
                     version: str | None = None, conn=None) -> list[Path]:
    tables = inventory_tables(project_id, doc_id, version, conn)
    return write_tables(tables, f"inventory_{doc_id or 'all'}", fmt, out_dir or default_out_dir(project_id),
                        columns={t: INV_FIELDS[t] for t in tables})


def export_findings(project_id: str, fmt: str, out_dir: Path | str | None = None, doc_id: str | None = None,
                    version: str | None = None, conn=None) -> list[Path]:
    rows = findings_rows(project_id, doc_id, version, conn)
    cols = ["id", "rule_id", "severity", "title", "explanation", "entities", "label"]
    return write_tables({"findings": rows}, f"findings_{doc_id or 'all'}", fmt,
                        out_dir or default_out_dir(project_id), columns={"findings": cols})


def export_diff(diff: Diff, fmt: str, out_dir: Path | str | None = None) -> list[Path]:
    return write_tables(diff_tables(diff), f"diff_{diff.doc_a}__{diff.doc_b}", fmt,
                        out_dir or default_out_dir(diff.project_id))


def export_report(report: dict, fmt: str, out_dir: Path | str, name: str = "report") -> list[Path]:
    """report may hold "rows" (list of dicts, e.g. component report rows) or "tables" ({name: rows});
    otherwise its scalar fields become one table of key/value rows. report["citations"] (list of
    {doc_id, version, page, section}) is attached to the key/value rows when present."""
    if "tables" in report:
        tables = {k: list(v) for k, v in report["tables"].items()}
    elif "rows" in report:
        tables = {"report": list(report["rows"])}
    else:
        cites = report.get("citations") or [{}]
        status = report.get("review_status", "unreviewed")
        tables = {"report": [{"field": k, "value": v, "review_status": status, **{c: ci.get(c) for c in CITE_COLS}}
                             for k, v in report.items() if k not in ("citations", "review_status")
                             for ci in cites[:1]]}
    return write_tables(tables, name, fmt, out_dir)


def _c(cit: dict | None) -> dict:
    cit = cit or {}
    return {"doc_id": cit.get("doc_id"), "version": cit.get("version"), "page": cit.get("page"),
            "section": cit.get("section")}


def report_tables(report: dict) -> dict[str, list[dict]]:
    """Tidy tables (every row with review_status + doc/version/page/section citation) for a stored report dict
    produced by app.summarize (component_report / document_summary / flow_summary)."""
    st = report.get("review_status", "unreviewed")
    kind = report.get("kind")
    meta = {"kind": kind, "target": report.get("target"), "model": report.get("model"),
            "confidence": report.get("confidence"), "limitation": report.get("limitation", ""),
            "report_id": report.get("id"), "review_status": st}
    tables: dict[str, list[dict]] = {}
    if kind == "component_report":
        tables["purpose"] = ([{**meta, "text": report.get("purpose"), "label": ""}] if not report.get("purpose_citations")
                             else [{**meta, "text": report.get("purpose"), "label": x["label"], **_c(x),
                                    "snippet": x.get("snippet", "")} for x in report["purpose_citations"]])
        for key in ("ports", "interfaces", "upstream", "downstream", "signals_sent", "signals_received"):
            tables[key] = [{"name": i.get("name"), "detail": i.get("direction") or i.get("kind") or
                            ",".join(i.get("receivers", [])) or i.get("sender", ""), "review_status": st,
                            **_c(i.get("citation"))} for i in report.get(key, [])]
    elif kind == "document_summary":
        tables["summary"] = [{**meta, "text": report.get("summary"), "label": x["label"], **_c(x)}
                             for x in report.get("citations") or [{"label": ""}]]
        tables["bullets"] = [{"label": b["label"], "text": b["text"], "review_status": st, **_c(b.get("citation"))}
                             for b in report.get("bullets", [])]
    elif kind == "flow_summary":
        tables["steps"] = [{"flow": f["title"], "n": s["n"], "text": s["text"], "flagged": s.get("flagged", False),
                            "unknown_entities": s.get("unknown_entities", []), "review_status": st,
                            **_c(s.get("citation"))} for f in report.get("flows", []) for s in f["steps"]]
    else:
        tables["report"] = [{**meta, "content": str(report)[:2000]}]
    tables["meta"] = [meta]
    return tables


def export_stored_report(report: dict, fmt: str, out_dir: Path | str) -> list[Path]:
    return write_tables(report_tables(report), f"report_{report.get('kind')}_{report.get('id', 'x')}", fmt, out_dir)
