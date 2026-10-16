"""Deterministic mismatch (M) and completeness (C) rules over the extracted inventory. No LLM.

Every rule is a pure function ``rule_xx(inv, ...) -> list[Finding]`` where ``inv`` has the shape returned by
``app.extract.get_inventory`` (``{"components": [dict], "ports": [...], "interfaces": [...], "signals": [...]}``).

Rules
-----
M1 required port (with an interface) with no provided port of the same interface on another component.
M2 kind mismatch. Kind is represented in two places: the interface table (``Interface | Kind | ...``) and an
   optional ``Kind`` column of the port table (``Component | Port | Direction | Interface | Kind``). For every
   *defined* interface with a known kind, a port whose own known kind differs from the interface definition is
   a mismatch; so are provider/required ports of one interface whose known kinds differ from each other.
   One finding per interface (entities = interface + offending ports). Unknown kinds never count.
M3 signal with a sender but no receivers, or receivers but no sender.
M4 near-duplicate component names or near-duplicate interface names
   (rapidfuzz ratio on normalised names >= settings.fuzzy_threshold, names not identical once normalised).
C1 component named in none of the functional-flow sections (sections whose title matches /flow/i).
   Skipped when the document has no flow section at all (nothing to compare against).
C2 port with no interface.
C3 interface referenced by a port but never defined (no row in an interface table).
C4 component without a description.

``run_checks`` stores findings in SQLite table ``findings`` (idempotent per project/doc/version) and
``get_findings`` reads them back. Findings on approved (real) documents are labelled "potential, needs review".
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from app import controls
from app.config import settings
from app.extract import get_inventory, normalize_name

POTENTIAL_LABEL = "potential, needs review"
FLOW_TITLE = re.compile(r"functional flow|flow", re.I)
SEVERITY = {"M1": "high", "M2": "high", "M3": "medium", "M4": "low",
            "C1": "medium", "C2": "medium", "C3": "medium", "C4": "low"}


class Evidence(BaseModel):
    doc_id: str
    version: str | None = None
    page: int | None = None
    section: str = ""


class Finding(BaseModel):
    id: int | None = None
    project_id: str = ""
    doc_id: str = ""
    version: str | None = None
    rule_id: str
    severity: str = "medium"
    title: str
    explanation: str
    entities: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    review_status: str = "unreviewed"
    label: str = ""


# ---------------------------------------------------------------- helpers

def _ev(row: dict) -> Evidence:
    srcs = row.get("sources") or []
    s = srcs[0] if srcs else {}
    return Evidence(doc_id=s.get("doc_id") or row.get("doc_id") or "", version=s.get("version") or row.get("version"),
                    page=s.get("page", row.get("page")), section=s.get("section") or row.get("section") or "")


def _evs(*rows: dict) -> list[Evidence]:
    out: list[Evidence] = []
    for r in rows:
        e = _ev(r)
        if e not in out:
            out.append(e)
    return out


def _port_id(p: dict) -> str:
    return f"{p.get('component') or '?'}.{p['name']}"


def _mk(rule: str, title: str, expl: str, entities: list[str], rows: list[dict]) -> Finding:
    return Finding(rule_id=rule, severity=SEVERITY[rule], title=title, explanation=expl, entities=entities,
                   evidence=_evs(*rows))


# ---------------------------------------------------------------- rules

def rule_m1(inv: dict) -> list[Finding]:
    ports = inv.get("ports", [])
    out = []
    for p in ports:
        if p.get("direction") != "required" or not p.get("interface"):
            continue
        ni, nc = normalize_name(p["interface"]), normalize_name(p.get("component", ""))
        if any(q.get("direction") == "provided" and normalize_name(q.get("interface", "")) == ni
               and normalize_name(q.get("component", "")) != nc for q in ports):
            continue
        out.append(_mk("M1", f"Required port {_port_id(p)} has no provider",
                       f"Port {_port_id(p)} requires interface {p['interface']} but no other component provides it.",
                       [_port_id(p)], [p]))
    return out


def rule_m2(inv: dict) -> list[Finding]:
    ifaces = {i["norm_name"]: i for i in inv.get("interfaces", []) if i.get("kind") not in (None, "", "unknown")}
    by_iface: dict[str, list[dict]] = {}
    for p in inv.get("ports", []):
        if p.get("interface"):
            by_iface.setdefault(normalize_name(p["interface"]), []).append(p)
    out = []
    for ni, ports in by_iface.items():
        known = [p for p in ports if p.get("kind") not in (None, "", "unknown")]
        idef = ifaces.get(ni)
        bad = [p for p in known if idef and p["kind"] != idef["kind"]]
        kinds = {p["kind"] for p in known}
        if not bad and len(kinds) > 1:
            bad = known
        if not bad:
            continue
        name = idef["name"] if idef else ports[0]["interface"]
        detail = ", ".join(f"{_port_id(p)}={p['kind']}" for p in bad)
        expl = (f"Interface {name} is defined as {idef['kind']}" if idef else f"Interface {name}") + \
               f" but connected ports declare a different kind: {detail}."
        out.append(_mk("M2", f"Interface kind mismatch on {name}", expl,
                       [name] + [_port_id(p) for p in bad], ([idef] if idef else []) + bad))
    return out


def rule_m3(inv: dict) -> list[Finding]:
    out = []
    for s in inv.get("signals", []):
        sender, recv = (s.get("sender") or "").strip(), [r for r in (s.get("receivers") or []) if r]
        if sender and not recv:
            out.append(_mk("M3", f"Signal {s['name']} has no receivers",
                           f"Signal {s['name']} is sent by {sender} but no receiver is listed.", [s["name"]], [s]))
        elif recv and not sender:
            out.append(_mk("M3", f"Signal {s['name']} has no sender",
                           f"Signal {s['name']} is received by {', '.join(recv)} but no sender is listed.",
                           [s["name"]], [s]))
    return out


def _near_pairs(rows: list[dict], threshold: float) -> list[tuple[dict, dict, float]]:
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if a["norm_name"] == b["norm_name"]:
                continue
            score = fuzz.ratio(a["norm_name"], b["norm_name"])
            if score >= threshold:
                out.append((a, b, score))
    return out


def rule_m4(inv: dict, threshold: float | None = None) -> list[Finding]:
    th = settings.fuzzy_threshold if threshold is None else threshold
    out = []
    for kind, key in (("component", "components"), ("interface", "interfaces")):
        for a, b, score in _near_pairs(inv.get(key, []), th):
            out.append(_mk("M4", f"Near-duplicate {kind} names: {a['name']} / {b['name']}",
                           f"{kind.capitalize()} names {a['name']} and {b['name']} are {score:.0f}% similar; "
                           "they may be the same element spelled differently.", [a["name"], b["name"]], [a, b]))
    return out


def _mentions(text: str, name: str) -> bool:
    pat = r"(?<![A-Za-z0-9])" + r"\s*".join(re.escape(w) for w in name.split()) + r"(?![A-Za-z0-9])"
    return re.search(pat, text, re.I) is not None


def rule_c1(inv: dict, flows: list[dict]) -> list[Finding]:
    """flows: [{"section_id", "title", "page", "text"}] for the functional-flow sections."""
    if not flows:
        return []
    text = "\n".join(f.get("text", "") for f in flows)
    out = []
    for c in inv.get("components", []):
        if not _mentions(text, c["name"]):
            out.append(_mk("C1", f"Component {c['name']} is in no functional flow",
                           f"Component {c['name']} is not named in any of the {len(flows)} functional-flow sections.",
                           [c["name"]], [c]))
    return out


def rule_c2(inv: dict) -> list[Finding]:
    return [_mk("C2", f"Port {_port_id(p)} has no interface", f"Port {_port_id(p)} does not reference an interface.",
                [_port_id(p)], [p]) for p in inv.get("ports", [])
            if not (p.get("interface") or "").strip()
            and p.get("extraction_method", "table") in ("table", "heuristic")]  # diagram/llm/convention ports never carry an interface


def rule_c3(inv: dict) -> list[Finding]:
    defined = {i["norm_name"] for i in inv.get("interfaces", [])}
    seen: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for p in inv.get("ports", []):
        n = normalize_name(p.get("interface", ""))
        if n and n not in defined:
            seen.setdefault(n, []).append(p)
            names.setdefault(n, p["interface"])
    return [_mk("C3", f"Interface {names[n]} is used but never defined",
                f"Interface {names[n]} is referenced by {', '.join(_port_id(p) for p in ps)} but has no definition.",
                [names[n]], ps) for n, ps in seen.items()]


def rule_c4(inv: dict) -> list[Finding]:
    return [_mk("C4", f"Component {c['name']} has no description", f"Component {c['name']} lacks a description.",
                [c["name"]], [c]) for c in inv.get("components", []) if not (c.get("description") or "").strip()]


RULES: dict[str, Callable[..., list[Finding]]] = {
    "M1": rule_m1, "M2": rule_m2, "M3": rule_m3, "M4": rule_m4, "C2": rule_c2, "C3": rule_c3, "C4": rule_c4}


def run_rules(inv: dict, flows: list[dict]) -> list[Finding]:
    """Pure: run every rule on an inventory (+ flow sections)."""
    out: list[Finding] = []
    for rid in ("M1", "M2", "M3", "M4"):
        out += RULES[rid](inv)
    out += rule_c1(inv, flows)
    for rid in ("C2", "C3", "C4"):
        out += RULES[rid](inv)
    return out


def flow_sections(doc: dict) -> list[dict]:
    return [{"section_id": s["section_id"], "title": s["title"], "page": s["page_start"], "text": s.get("text", "")}
            for s in doc["sections"] if s["section_id"] != "0" and FLOW_TITLE.search(s["title"])]


# ---------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, doc_id TEXT NOT NULL, version TEXT,
  rule_id TEXT NOT NULL, severity TEXT, title TEXT, explanation TEXT, entities_json TEXT, evidence_json TEXT,
  label TEXT DEFAULT '', review_status TEXT NOT NULL DEFAULT 'unreviewed');
CREATE INDEX IF NOT EXISTS idx_findings_doc ON findings(project_id, doc_id);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def _row_to_finding(r: Any) -> Finding:
    return Finding(id=r["id"], project_id=r["project_id"], doc_id=r["doc_id"], version=r["version"],
                   rule_id=r["rule_id"], severity=r["severity"], title=r["title"], explanation=r["explanation"],
                   entities=json.loads(r["entities_json"] or "[]"),
                   evidence=[Evidence(**e) for e in json.loads(r["evidence_json"] or "[]")],
                   label=r["label"] or "", review_status=r["review_status"])


def run_checks(project_id: str, doc_id: str, version: str | None = None, conn=None,
               processed_dir=None) -> list[Finding]:
    """Run all rules for one document, replace its stored findings, return them (with ids)."""
    from app.ingest import load_processed
    doc = load_processed(project_id, doc_id, processed_dir)
    version = version or doc.get("version")
    with controls._use(conn) as c:
        ensure_schema(c)
        inv = get_inventory(project_id, doc_id, version, c, include_rejected=False)
        label = POTENTIAL_LABEL if doc.get("source_status") == "approved" else ""
        findings = run_rules(inv, flow_sections(doc))
        existing = {_finding_key(_row_to_finding(r)): r for r in c.execute(
            "SELECT * FROM findings WHERE project_id=? AND doc_id=? AND version IS ?", (project_id, doc_id, version))}
        seen = set()
        for f in findings:
            f.project_id, f.doc_id, f.version, f.label = project_id, doc_id, version, label
            previous = existing.get(_finding_key(f))
            if previous:
                f.id, f.review_status = previous["id"], previous["review_status"]
                seen.add(f.id)
                continue
            cur = c.execute(
                "INSERT INTO findings(project_id, doc_id, version, rule_id, severity, title, explanation, "
                "entities_json, evidence_json, label, review_status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, doc_id, version, f.rule_id, f.severity, f.title, f.explanation,
                 json.dumps(f.entities), json.dumps([e.model_dump() for e in f.evidence]), label, f.review_status))
            f.id = cur.lastrowid
            seen.add(f.id)
        for previous in existing.values():
            if previous["id"] not in seen:
                # Review history is retained separately; obsolete issues leave the active list.
                c.execute("DELETE FROM findings WHERE id=?", (previous["id"],))
        c.commit()
    return findings


def _finding_key(f: Finding) -> tuple:
    return (f.rule_id, f.severity, f.title, f.explanation, tuple(sorted(f.entities)),
            tuple(sorted((e.doc_id, e.version or "", e.page or 0, e.section) for e in f.evidence)))


def get_findings(project_id: str, doc_id: str | None = None, version: str | None = None,
                 rule_id: str | None = None, conn=None) -> list[Finding]:
    with controls._use(conn) as c:
        ensure_schema(c)
        q, args = "SELECT * FROM findings WHERE project_id=?", [project_id]
        for col, val in (("doc_id", doc_id), ("version", version), ("rule_id", rule_id)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        return [_row_to_finding(r) for r in c.execute(q + " ORDER BY id", args)]
