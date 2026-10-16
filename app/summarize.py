"""Reports built from the structured inventory and graph (deterministic) plus LLM stubs.

component_report: ports, interfaces, upstream/downstream, signals, source pages (every item cited).
  The "purpose" paragraph needs an LLM; without one it reads "LLM not configured" with confidence Low.
document_summary: LLM stub, degrades gracefully.
functional_flow_summary: deterministic ordered steps from flow sections, each cited, unknown names flagged.
Reports are stored in the ``reports`` table with review_status "unreviewed".
"""
from __future__ import annotations

import inspect
import json
import re
import sqlite3

from app import controls, llm, retrieve
from app.checks import _mentions, flow_sections
from app.extract import get_inventory, normalize_name
from app.graph import build_graph, component_flow_graph, dependencies_of, dependents_of
from app.ingest import load_processed

NOT_CONFIGURED = "LLM not configured"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, doc_id TEXT, version TEXT, kind TEXT,
  target TEXT, content_json TEXT, confidence TEXT, created_at TEXT,
  review_status TEXT NOT NULL DEFAULT 'unreviewed');
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def cite(row: dict | None, doc_id: str | None = None, version: str | None = None) -> dict:
    row = row or {}
    return {"doc_id": row.get("doc_id", doc_id), "version": row.get("version", version),
            "page": row.get("page"), "section": row.get("section")}


_LAB = re.compile(r"\[\s*([A-Z]\d+(?:\s*,\s*[A-Z]\d+)*)\s*\]")
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[])|\n+")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
MAX_OVERVIEW_SENTENCES = 6
SUPPORT_MIN = 0.4  # share of a sentence's content words that must appear in its cited source text
CHUNK_CHARS = 1800  # keeps one map call well inside num_ctx 4096


def _model_name(provider, model: str | None) -> str:
    return model or getattr(provider, "model", None) or getattr(provider, "name", "") or ""


def _gen(provider, prompt: str, system: str, model: str | None, purpose: str, chunk_ids: list | None = None) -> str:
    """provider.generate with model/purpose when supported; raises LLMNotConfigured."""
    params = inspect.signature(provider.generate).parameters
    kw = {k: v for k, v in (("model", model), ("purpose", purpose), ("chunk_ids", chunk_ids)) if k in params}
    out = provider.generate(prompt, system, **kw)
    return out if isinstance(out, str) else json.dumps(out)


_STOP = {"that", "this", "with", "from", "have", "has", "the", "and", "are", "for", "its", "their", "which", "also",
         "into", "such", "than", "will", "can", "not", "was", "were", "been", "used", "uses", "using", "component"}


def _words(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", t.lower()) if w not in _STOP}


def _support(sentence: str, source: str) -> float:
    w = _words(_LAB.sub("", sentence))
    return len(w & _words(source)) / len(w) if w else 1.0


def cited_filter(text: str, valid: set[str], texts: dict[str, str] | None = None,
                 names: tuple[str, ...] = ()) -> tuple[list[str], list[str], int]:
    """qa.postprocess-style filter for arbitrary labels ([C1], [F2], [S3]).

    Returns (kept_sentences, labels_in_order, n_dropped). A sentence is kept only if it carries at least one label in
    ``valid``; invented labels are removed from kept sentences; sentences without a valid label are dropped.
    """
    text = re.sub(r"[\(\[]\s*([A-Z]\d+(?:\s*,\s*[A-Z]\d+)*)\s*[\)\]]", r"[\1]", text)  # (F1) -> [F1]
    text = re.sub(r"\]\s*\[(?=\s*[A-Z]\d)", ", ", text)
    text = re.sub(r"([.!?])\s*(\[\s*[A-Z]\d+(?:\s*,\s*[A-Z]\d+)*\s*\])", r" \2\1", text)
    kept, labels, dropped = [], [], 0
    for sent in _SPLIT.split(text):
        sent = _BULLET.sub("", (sent or "").strip())
        if not re.sub(r"[\W_]+", "", sent):
            continue
        found = [x for m in _LAB.finditer(sent) for x in re.split(r"\s*,\s*", m.group(1)) if x in valid]
        if not found:
            dropped += 1
            continue

        def fix(m):
            ok = [x for x in re.split(r"\s*,\s*", m.group(1)) if x in valid]
            return f"[{', '.join(ok)}]" if ok else ""
        clean = re.sub(r"\s+([.!?,])", r"\1", _LAB.sub(fix, sent)).strip()
        if texts:  # lexical support check + citation repair (labels are wrong more often than claims)
            body = _LAB.sub("", clean)
            for n in names:
                body = re.sub(re.escape(n), "", body, flags=re.I)
            best = max(valid & set(texts), key=lambda x: _support(body, texts[x]), default=None)
            cur = max((_support(body, texts[x]) for x in found if x in texts), default=0.0)
            if cur < SUPPORT_MIN:
                if best is not None and _support(body, texts[best]) >= SUPPORT_MIN:
                    found = [best]
                    clean = _LAB.sub("", clean).rstrip(" .!?") + f" [{best}]."
                else:
                    dropped += 1
                    continue
        kept.append(clean)
        labels += [x for x in found if x not in labels]
    return kept, labels, dropped


def report_confidence(n_kept: int, n_dropped: int, n_labels: int, has_chunk_evidence: bool = True) -> str:
    """Rule-based (like qa.compute_confidence): Low if nothing survives or <50% of sentences cited;
    High if >=80% cited, >=2 distinct sources and chunk evidence; else Medium."""
    total = n_kept + n_dropped
    share = n_kept / total if total else 0.0
    if n_kept == 0 or share < 0.5:
        return "Low"
    if share >= 0.8 and n_labels >= 2 and has_chunk_evidence:
        return "High"
    return "Medium"


COMP_SYSTEM = (
    "You write the purpose of one software component for an architecture review, using ONLY the labelled evidence "
    "given: chunks [C1].. and facts [F1].. . Rules: 1) Write 2-4 short sentences. 2) End EVERY sentence with the "
    "label(s) it comes from, like [C1] or [F2, C1]. 3) No outside knowledge; do not invent names. 4) If the evidence "
    "says little, write fewer sentences. Example: The FooManager controls the lamp [C2]. It has 3 ports [F3].")


def _component_purpose(c, project_id, doc_id, comp, ports, up, down, sent, recv, provider, model, chroma_dir):
    """(purpose_text, purpose_citations, confidence, limitation). Degrades to NOT_CONFIGURED / Low."""
    provider = provider or llm.get_provider()
    if isinstance(provider, llm.NullProvider):
        return NOT_CONFIGURED, [], "Low", "No LLM configured; only structured facts are shown."
    hits = retrieve.search(project_id, f"{comp['name']} {comp.get('description', '')}".strip(), k=4,
                           filters={"doc_id": doc_id}, chroma_dir=chroma_dir)
    lab: dict[str, dict] = {}
    parts = []
    for i, h in enumerate(hits, 1):
        m = h["metadata"]
        lab[f"C{i}"] = {"kind": "chunk", "doc_id": m.get("doc_id"), "version": m.get("version"),
                        "page": m.get("page_start"), "section": f"{m.get('section_id')} {m.get('section_title')}",
                        "snippet": h["text"][:200], "chunk_id": h["id"], "text": h["text"]}
        parts.append(f"[C{i}] (p.{m.get('page_start')}, section {m.get('section_id')} {m.get('section_title')})\n"
                     f"{h['text'][:700]}")
    facts = []

    def fact(text, citation):
        n = len(facts) + 1
        facts.append(f"[F{n}] {text}")
        lab[f"F{n}"] = {"kind": "fact", **citation, "snippet": text[:200], "text": text}
    if comp.get("description"):
        fact(f"{comp['name']} description: {comp['description'][:300]}", cite(comp))
    if comp.get("category"):
        fact(f"{comp['name']} category: {comp['category']}", cite(comp))
    if ports:
        fact(f"{comp['name']} has ports: " + ", ".join(f"{p['name']} ({p['direction']})" for p in ports[:12]),
             ports[0]["citation"])
    if up:
        fact(f"{comp['name']} depends on: " + ", ".join(x["name"] for x in up[:10]), up[0]["citation"])
    if down:
        fact(f"{comp['name']} is used by: " + ", ".join(x["name"] for x in down[:10]), down[0]["citation"])
    if sent:
        fact(f"{comp['name']} sends signals: " + ", ".join(x["name"] for x in sent[:10]), sent[0]["citation"])
    if recv:
        fact(f"{comp['name']} receives signals: " + ", ".join(x["name"] for x in recv[:10]), recv[0]["citation"])
    if not parts and not facts:
        return "Not stated in the provided documents.", [], "Low", "No evidence found for this component."
    prompt = ("EVIDENCE CHUNKS:\n" + "\n\n".join(parts) + "\n\nFACTS:\n" + "\n".join(facts) +
              f"\n\nTASK: Describe the purpose of {comp['name']} (every sentence ends with a label).")
    try:
        text = _gen(provider, prompt, COMP_SYSTEM, model, "component_report", [h["id"] for h in hits])
    except (llm.LLMNotConfigured, NotImplementedError):
        return NOT_CONFIGURED, [], "Low", "LLM unreachable; only structured facts are shown."
    kept, labels, dropped = cited_filter(text, set(lab), {k: v["text"] for k, v in lab.items()}, (comp["name"],))
    if not kept:
        return ("Not stated in the provided documents.", [], "Low",
                "The model produced no sentence with a valid citation, so the purpose was withheld.")
    cites = [{"label": x, **{k: v for k, v in lab[x].items() if k != "text"}} for x in labels]
    conf = report_confidence(len(kept), dropped, len(labels), any(x.startswith("C") for x in labels))
    limitation = "Some uncited sentences were removed." if dropped else ""
    if not hits:
        limitation = (limitation + " No document chunks retrieved; based on structured facts only.").strip()
    return " ".join(kept), cites, conf, limitation


def _store(c: sqlite3.Connection, report: dict) -> dict:
    ensure_schema(c)
    cur = c.execute("INSERT INTO reports(project_id,doc_id,version,kind,target,content_json,confidence,created_at,"
                    "review_status) VALUES(?,?,?,?,?,?,?,?,?)",
                    (report["project_id"], report["doc_id"], report["version"], report["kind"],
                     report.get("target", ""), json.dumps(report, default=str), report["confidence"],
                     controls._now(), report["review_status"]))
    c.commit()
    report["id"] = cur.lastrowid
    return report


def component_report(project_id: str, doc_id: str, version: str | None, component: str,
                     conn: sqlite3.Connection | None = None, provider=None, model: str | None = None,
                     chroma_dir=None) -> dict:
    """Structured report for one component. Raises ValueError if the component is not in the inventory."""
    with controls._use(conn) as c:
        inv = get_inventory(project_id, doc_id, version, c, include_rejected=False)
        norm = normalize_name(component)
        comp = next((x for x in inv["components"] if x["norm_name"] == norm), None)
        if comp is None:
            raise ValueError(f"unknown component {component!r} in {doc_id}")
        g = build_graph(project_id, doc_id, version, c)
        by_norm = {x["norm_name"]: x for x in inv["components"]}
        ports = [p for p in inv["ports"] if normalize_name(p.get("component") or "") == norm]
        ifaces = {i["norm_name"]: i for i in inv["interfaces"]}
        port_items = [{"name": p["name"], "direction": p.get("direction", "unknown"),
                       "interface": p.get("interface", ""), "citation": cite(p)} for p in ports]
        iface_items, seen = [], set()
        for p in ports:
            n = normalize_name(p.get("interface") or "")
            if n and n not in seen:
                seen.add(n)
                d = ifaces.get(n)
                iface_items.append({"name": d["name"] if d else p["interface"],
                                    "kind": d.get("kind", "unknown") if d else "undefined",
                                    "citation": cite(d or p)})
        up = [{"name": n, "citation": cite(by_norm.get(normalize_name(n)))} for n in dependencies_of(g, comp["name"])]
        down = [{"name": n, "citation": cite(by_norm.get(normalize_name(n)))} for n in dependents_of(g, comp["name"])]
        sent = [{"name": s["name"], "receivers": s.get("receivers", []), "citation": cite(s)}
                for s in inv["signals"] if normalize_name(s.get("sender") or "") == norm]
        recv = [{"name": s["name"], "sender": s.get("sender", ""), "citation": cite(s)}
                for s in inv["signals"] if norm in {normalize_name(r) for r in s.get("receivers", [])}]
        items = [{"citation": cite(comp)}] + port_items + iface_items + up + down + sent + recv
        pages = sorted({x["citation"]["page"] for x in items if x["citation"]["page"] is not None})
        provider = provider or llm.get_provider()
        purpose, pcites, conf, limitation = _component_purpose(
            c, project_id, doc_id, comp, port_items, up, down, sent, recv, provider, model, chroma_dir)
        report = {"kind": "component_report", "project_id": project_id, "doc_id": doc_id,
                  "version": version or comp.get("version"), "component": comp["name"], "target": comp["name"],
                  "description": comp.get("description", ""), "category": comp.get("category", ""),
                  "citation": cite(comp), "purpose": purpose, "purpose_citations": pcites,
                  "model": _model_name(provider, model), "limitation": limitation, "confidence": conf,
                  "ports": port_items, "interfaces": iface_items, "upstream": up, "downstream": down,
                  "signals_sent": sent, "signals_received": recv, "source_pages": pages,
                  "review_status": "unreviewed",
                  "note": "Structured sections come from the extracted inventory; the purpose paragraph is LLM-written "
                          "from retrieved chunks and facts, uncited sentences removed."}
        return _store(c, report)


_SKIP_TITLE = re.compile(r"^(table of contents|contents|list of (figures|tables)|document change history|"
                         r"disclaimer|legal|references?|bibliography)\b", re.I)
MAP_SYSTEM = (
    "You summarise one passage of a technical architecture document using ONLY the passage. Write 2-3 bullet "
    "points, one sentence each, and end EVERY bullet with the passage label like [S1]. No outside knowledge. "
    "Example: - The FooManager controls the lamp [S1].")
REDUCE_SYSTEM = (
    "You write a short overview (3-5 sentences) of a document from labelled bullet points. Use ONLY the bullets. "
    "End EVERY sentence with the label(s) of the bullets it is based on, like [S2] or [S2, S5]. Do not invent "
    "labels or facts.")


def _pieces(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split section text into pieces of at most ~size chars on paragraph/sentence boundaries."""
    text = re.sub(r"[ \t]+", " ", text).strip()
    out, cur = [], ""
    for para in re.split(r"(?<=[.!?])\s+|\n{2,}", text):
        if cur and len(cur) + len(para) + 1 > size:
            out.append(cur)
            cur = ""
        cur = (cur + " " + para).strip()
    if cur:
        out.append(cur)
    return out


def document_summary(project_id: str, doc_id: str, version: str | None = None,
                     conn: sqlite3.Connection | None = None, processed_dir=None, provider=None,
                     model: str | None = None, max_pieces_per_section: int = 3, min_chars: int = 150) -> dict:
    """Map-reduce summary. Map: 2-3 cited bullets per section piece (label S<n> = that piece's citation).
    Reduce: short overview citing the S labels; invented/uncited sentences dropped. Skips front matter / TOC.
    Degrades to 'LLM not configured' / Low."""
    with controls._use(conn) as c:
        doc = load_processed(project_id, doc_id, processed_dir)
        version = version or doc.get("version")
        provider = provider or llm.get_provider()
        titles = [f"{s['section_id']} {s['title']}" for s in doc["sections"] if s["section_id"] != "0"]
        base = {"kind": "document_summary", "project_id": project_id, "doc_id": doc_id, "version": version,
                "target": doc_id, "sections": titles, "model": _model_name(provider, model),
                "review_status": "unreviewed"}
        if isinstance(provider, llm.NullProvider):
            return _store(c, {**base, "summary": NOT_CONFIGURED, "confidence": "Low", "bullets": [],
                              "citations": [], "limitation": "No LLM configured."})
        labels: dict[str, dict] = {}
        bullets: list[dict] = []
        n_kept = n_drop = n_skipped = 0
        try:
            for s in doc["sections"]:
                text = (s.get("text") or "").strip()
                if s["section_id"] == "0" or _SKIP_TITLE.match(s["title"].strip()) or len(text) < min_chars:
                    n_skipped += 1
                    continue
                if re.search(r"(\.\s?){6,}", text):  # dotted TOC leaders
                    n_skipped += 1
                    continue
                for piece in _pieces(text)[:max_pieces_per_section]:
                    lab = f"S{len(labels) + 1}"
                    cit = {"doc_id": doc_id, "version": version, "page": s["page_start"],
                           "section": f"{s['section_id']} {s['title']}"}
                    prompt = (f"PASSAGE [{lab}] (section {s['section_id']} {s['title']}, p.{s['page_start']}):\n"
                              f"{piece}\n\nBULLETS (each ends with [{lab}]):")
                    out = _gen(provider, prompt, MAP_SYSTEM, model, "doc_summary_map")
                    kept, _, dropped = cited_filter(out, {lab})
                    n_kept, n_drop = n_kept + len(kept), n_drop + dropped
                    if kept:
                        labels[lab] = {"label": lab, **cit, "snippet": piece[:200]}
                        bullets += [{"label": lab, "text": k, "citation": cit} for k in kept[:3]]
            if not bullets:
                return _store(c, {**base, "summary": "Not stated in the provided documents.", "confidence": "Low",
                                  "bullets": [], "citations": [],
                                  "limitation": "No section produced a validly cited bullet."})
            # reduce: keep the prompt small (first bullet per section if everything does not fit)
            listing = "\n".join(f"- {b['text']}" for b in bullets)
            if len(listing) > 7000:
                seen, short = set(), []
                for b in bullets:
                    if b["label"] not in seen:
                        seen.add(b["label"])
                        short.append(f"- {b['text']}")
                listing = "\n".join(short)[:7000]
            valid = set(labels)
            out = _gen(provider, f"BULLETS:\n{listing}\n\nOVERVIEW (every sentence ends with a label):",
                       REDUCE_SYSTEM, model, "doc_summary_reduce")
        except (llm.LLMNotConfigured, NotImplementedError):
            return _store(c, {**base, "summary": NOT_CONFIGURED, "confidence": "Low", "bullets": [],
                              "citations": [], "limitation": "LLM unreachable."})
        kept, used, dropped = cited_filter(out, valid)
        kept = kept[:MAX_OVERVIEW_SENTENCES]
        used = [x for x in used if any(f"{x}]" in k or f"{x}," in k for k in kept)]
        total = n_kept + n_drop
        limitation = []
        if n_drop:
            limitation.append(f"{n_drop} uncited map sentence(s) removed.")
        if dropped:
            limitation.append(f"{dropped} uncited overview sentence(s) removed.")
        limitation.append(f"{n_skipped} front-matter/TOC/short section(s) skipped; at most "
                          f"{max_pieces_per_section} passages per section summarised.")
        conf = report_confidence(len(kept), dropped, len(used)) if kept else "Low"
        if conf == "High" and total and n_drop / (total + n_drop) > 0.2:
            conf = "Medium"
        report = {**base, "summary": " ".join(kept) if kept else "Not stated in the provided documents.",
                  "bullets": bullets, "citations": [labels[x] for x in used], "confidence": conf,
                  "limitation": " ".join(limitation)}
        return _store(c, report)


_STEP = re.compile(r"(?m)^\s*(\d+)[.)]\s+")
_CAMEL = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b")


def parse_steps(text: str) -> list[str]:
    """Ordered steps: numbered lines if present, else sentences."""
    parts = _STEP.split(text)
    if len(parts) > 1:
        steps = [re.sub(r"\s+", " ", parts[i]).strip() for i in range(2, len(parts), 2)]
    else:
        steps = [re.sub(r"\s+", " ", s).strip() for s in re.split(r"(?<=[.!?])\s+", text)]
    return [s for s in steps if s]


MAX_GRAPH_FLOWS, MAX_STEPS_PER_FLOW = 10, 15


def _edge_evidence(inv: dict, a: str, b: str) -> tuple[str, dict] | None:
    """(description, citation) of the signal/port link a -> b (diagram source page), or None."""
    na, nb = normalize_name(a), normalize_name(b)
    for s in inv["signals"]:
        senders = {normalize_name(x) for x in [s.get("sender", "")] + list(s.get("extra_senders") or [])}
        if na in senders and nb in {normalize_name(r) for r in s.get("receivers", [])}:
            return f"{a} sends signal {s['name']} to {b}", cite(s)
    for p in inv["ports"]:
        if normalize_name(p.get("component") or "") == na and p.get("direction") == "provided" and p.get("interface"):
            for q in inv["ports"]:
                if (normalize_name(q.get("component") or "") == nb and q.get("direction") == "required"
                        and normalize_name(q.get("interface") or "") == normalize_name(p["interface"])):
                    return f"{a} provides {p['interface']} (port {p['name']}) to {b} (port {q['name']})", cite(p)
    return None


def _graph_flows(project_id, doc_id, version, inv, c) -> tuple[list[dict], set[str], int]:
    """Flows derived from component-level graph paths: one flow per source component, BFS edge order."""
    g = build_graph(project_id, doc_id, version, c)
    f = component_flow_graph(g)
    descr = {x["norm_name"]: x for x in inv["components"]}
    inferred = {d["name"] for _, d in g.nodes(data=True) if d["type"] == "component" and d.get("inferred")}
    sources = sorted((n for n in f if f.in_degree(n) == 0 and f.out_degree(n) > 0),
                     key=lambda n: -len(__import__("networkx").descendants(f, n)))
    if not sources:  # cycles only: start from the busiest nodes
        sources = sorted((n for n in f if f.out_degree(n)), key=lambda n: -f.out_degree(n))
    flows, referenced, flagged, used = [], set(), 0, set()
    for src in sources:
        if len(flows) >= MAX_GRAPH_FLOWS:
            break
        steps, seen = [], {src}
        frontier = [src]
        while frontier and len(steps) < MAX_STEPS_PER_FLOW:
            nxt = []
            for u in frontier:
                for v in sorted(f.successors(u)):
                    if (u, v) in used or len(steps) >= MAX_STEPS_PER_FLOW:
                        continue
                    used.add((u, v))
                    a, b = g.nodes[u]["name"], g.nodes[v]["name"]
                    ev = _edge_evidence(inv, a, b)
                    text, cit = ev if ev else (f"{a} feeds {b}", {"doc_id": doc_id, "version": version,
                                                                    "page": None, "section": None})
                    unknown = sorted(n for n in (a, b) if n in inferred)
                    referenced.update((a, b))
                    flagged += bool(unknown)
                    steps.append({"n": len(steps) + 1, "text": text, "components": [a, b], "unknown_entities": unknown,
                                  "flagged": bool(unknown), "citation": cit, "derived_from": "graph",
                                  "context": [descr[normalize_name(x)].get("description", "")[:160]
                                              for x in (a, b) if normalize_name(x) in descr
                                              and descr[normalize_name(x)].get("description")]})
                    if v not in seen:
                        seen.add(v)
                        nxt.append(v)
            frontier = nxt
        if steps:
            name = g.nodes[src]["name"]
            flows.append({"section_id": "", "title": f"Data flow from {name} (derived from diagram graph)",
                          "steps": steps})
    return flows, referenced, flagged


def functional_flow_summary(project_id: str, doc_id: str, version: str | None = None,
                            conn: sqlite3.Connection | None = None, processed_dir=None) -> dict:
    """Deterministic flow summary: flows[{title, steps[{n, text, citation, components, unknown_entities}]}]."""
    with controls._use(conn) as c:
        doc = load_processed(project_id, doc_id, processed_dir)
        version = version or doc.get("version")
        inv = get_inventory(project_id, doc_id, version, c, include_rejected=False)
        known = {x["norm_name"] for t in ("components", "ports", "interfaces", "signals") for x in inv.get(t, [])}
        known |= {normalize_name(p["name"]) for p in inv.get("ports", [])}
        comps = [x["name"] for x in inv.get("components", [])]
        flows, flagged, referenced = [], 0, set()
        for fs in flow_sections(doc):
            steps = []
            for n, text in enumerate(parse_steps(fs["text"]), 1):
                mentioned = [x for x in comps if _mentions(text, x)]
                unknown = sorted({t for t in _CAMEL.findall(text) if normalize_name(t) not in known})
                referenced.update(mentioned)
                flagged += bool(unknown)
                steps.append({"n": n, "text": text, "components": mentioned, "unknown_entities": unknown,
                              "flagged": bool(unknown),
                              "citation": {"doc_id": doc_id, "version": version, "page": fs["page"],
                                           "section": f"{fs['section_id']} {fs['title']}"}})
            if steps:
                flows.append({"section_id": fs["section_id"], "title": fs["title"], "steps": steps})
        source = "sections"
        if not flows:
            flows, referenced, flagged = _graph_flows(project_id, doc_id, version, inv, c)
            source = "graph" if flows else "none"
        n_steps = sum(len(f["steps"]) for f in flows)
        n_uncited = sum(1 for f in flows for st in f["steps"] if st["citation"].get("page") is None)
        if not flows:
            limitation = "No flow sections and no component-level graph edges; nothing to summarise."
        elif source == "graph":
            limitation = (f"No flow sections found; {n_steps} steps derived from diagram graph edges "
                          f"(capped at {MAX_GRAPH_FLOWS} flows x {MAX_STEPS_PER_FLOW} steps). Direction of "
                          "convention-inferred links is uncertain.")
        else:
            limitation = ""
        report = {"kind": "flow_summary", "source": source, "model": "", "limitation": limitation, "project_id": project_id, "doc_id": doc_id, "version": version,
                  "target": doc_id, "flows": flows, "components_referenced": sorted(referenced),
                  "flagged_steps": flagged,
                  "confidence": "Medium" if (flows and source == "sections") else "Low",
                  "review_status": "unreviewed",
                  "note": "Deterministic parse; flagged steps name entities missing from the inventory."}
        return _store(c, report)
