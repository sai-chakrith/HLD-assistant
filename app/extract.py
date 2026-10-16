"""Entity extraction: components, ports, interfaces and signals, with method + confidence + exact-page sources.

Layers (merge precedence table > diagram > heuristic > llm > convention; all sources are kept):
- table      header-alias classified tables (High)
- heuristic  headings / sentence / label regexes, prose port sentences (Medium)
- diagram    app.diagram vector-geometry extraction from the PDF (Medium/High)
- llm        one section per call, evidence quote must occur verbatim in the section (Medium)
- convention name-prefix inference of roles / port direction (Low/Medium, never overrides)

Public API
----------
- Models: Source(doc_id, version, page, section, method, evidence), Component, Port, Interface, Signal
  (every entity: extraction_method, confidence, review_status)
- HEADER_ALIASES, map_headers(header), classify_table(table)
- extract_entities(doc, use_llm=False, pdf_path=None, use_diagram=True, use_convention=True, provider=None,
  model=None, stats=None) -> {"components": [...], "ports": [...], "interfaces": [...], "signals": [...]}
- extract_from_prose(section, doc_id, version, provider=None, model=None, stats=None) -> list
- extract_document(project_id, doc_id, conn=None, processed_dir=None, use_llm=False, pdf_path=None, use_diagram=True,
  use_convention=True, provider=None, model=None, stats=None) -> counts dict; idempotent AND review-preserving
- method_counts(project_id, doc_id, conn=None), get_inventory(project_id, doc_id=None, version=None, conn=None)
- normalize_name(name)
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app import controls, headers, llm
from app.config import settings
from app.ingest import load_processed

ENTITY_TABLES = {"component": "components", "port": "ports", "interface": "interfaces", "signal": "signals"}
# precedence when merging: table > diagram > heuristic (headings/regex) > llm > convention
_METHOD_RANK = {"table": 5, "diagram": 4, "heuristic": 3, "llm": 2, "convention": 1}
Method = Literal["table", "heuristic", "diagram", "llm", "convention"]
Confidence = Literal["High", "Medium", "Low"]
_CONF_RANK = {"Low": 0, "Medium": 1, "High": 2}


# ---------------------------------------------------------------- models

class Source(BaseModel):
    doc_id: str
    version: str | None = None
    page: int | None = None
    section: str = ""
    method: str = ""        # extraction method that produced this source
    evidence: str = ""      # llm: verbatim quote (<= 25 words) from the section
    bbox: list[float] | None = None  # PDF points, when the source is a diagram label/box
    ocr: bool = False       # diagram page whose labels had to be read by OCR (weaker than a text-layer source)


class _Entity(BaseModel):
    name: str
    description: str = ""
    sources: list[Source] = Field(default_factory=list)
    extraction_method: Method = "table"
    confidence: Confidence = "High"
    review_status: str = "unreviewed"

    entity_type: str = ""

    @property
    def norm_name(self) -> str:
        return normalize_name(self.name)

    def field_dict(self) -> dict[str, Any]:
        skip = {"name", "sources", "extraction_method", "confidence", "review_status", "entity_type"}
        return {k: v for k, v in self.model_dump().items() if k not in skip}


class Component(_Entity):
    entity_type: str = "component"
    category: str = ""
    children: list[str] = Field(default_factory=list)  # category "composition": the components it groups


class Port(_Entity):
    entity_type: str = "port"
    component: str = ""
    direction: Literal["provided", "required", "unknown"] = "unknown"
    interface: str = ""
    kind: Literal["sender_receiver", "client_server", "unknown"] = "unknown"  # optional port-side kind


class Interface(_Entity):
    entity_type: str = "interface"
    kind: Literal["sender_receiver", "client_server", "unknown"] = "unknown"
    data_elements: list[str] = Field(default_factory=list)


class Signal(_Entity):
    entity_type: str = "signal"
    sender: str = ""
    receivers: list[str] = Field(default_factory=list)
    data_type: str = ""
    extra_senders: list[str] = Field(default_factory=list)  # same signal drawn from several senders (diagram)
    sender_role: str = ""     # convention: role implied by the name prefix (Cmd -> master ...)
    receiver_role: str = ""


MODELS = {"component": Component, "port": Port, "interface": Interface, "signal": Signal}


def normalize_name(name: str) -> str:
    """Lowercase, strip every non-alphanumeric char (so 'Interior Light-Manager' == 'interiorlightmanager')."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def is_junk_name(name: str) -> bool:
    """True for names that cannot be an architecture element: fewer than 3 letters, or mostly punctuation/digits
    (e.g. ': : :1 00 1234', ': Process :'). Applied before entities are stored."""
    n = (name or "").strip()
    letters = sum(ch.isalpha() for ch in n)
    core = sum(not ch.isspace() for ch in n)
    return letters < 3 or core == 0 or letters / core < 0.5 or n[:1] in ":;,|"


# ---------------------------------------------------------------- header aliases

def _hn(s: str) -> str:
    return headers.hn(s)


HEADER_ALIASES = headers.HEADER_ALIASES  # canonical field -> synonym list (shared with app.ingest)


def map_headers(header: list[str]) -> dict[str, int]:
    """Map canonical field -> column index (first occurrence wins). Header words are matched fuzzily
    (app.headers.match_header: exact alias, head-noun rules, rapidfuzz); unknown headers are ignored."""
    out: dict[str, int] = {}
    for i, h in enumerate(header):
        canon = headers.match_header(h)
        if canon and canon not in out:
            out[canon] = i
    return out


def _fill_inferred(cm: dict[str, int], table: dict) -> dict[str, int]:
    """Add columns whose role is clear from the cell values alone (never overrides a header-mapped column)."""
    inferred = headers.infer_roles(table.get("rows", []))
    used = set(cm.values())
    out = dict(cm)
    for role, i in inferred.items():
        if role not in out and i not in used:
            out[role] = i
            used.add(i)
    return out


def classify_table(table: dict) -> tuple[str | None, dict[str, int]]:
    """Return (entity_type or None, colmap) for a processed table dict."""
    if table.get("kind") in ("change_history",):
        return None, {}
    header = table.get("header") or []
    cm = map_headers(header)
    if table.get("kind") == "table" and len(cm) < len(header):
        cm = _fill_inferred(cm, table)  # unknown header words: infer the role from the cell values
    if "signal" not in cm and "data_elements" in cm and cm.keys() & {"sender", "receivers"}:
        cm["signal"] = cm.pop("data_elements")  # 'Data Element | Producer | Consumer(s)' lists signals
    f = set(cm)
    if "signal" in f and ("sender" in f or "receivers" in f):
        return "signal", cm
    if "port" in f and f & {"component", "direction", "interface", "kind"}:
        if "kind" in f and "direction" not in f:
            cm["direction"] = cm.pop("kind")
        return "port", cm
    if "interface" in f and f & {"kind", "data_elements", "description"} and "component" not in f:
        return "interface", cm
    if "component" in f and f & {"description", "category"}:
        return "component", cm
    return None, cm


_ILLUSTRATION = re.compile(r"^(single|multiple)\s*occur|^example", re.I)


def is_naming_illustration(table: dict) -> bool:
    """The BodyComfort naming-convention example (Port|Interface|Data element, rows 'Single occurrence'...)."""
    for row in table.get("rows", []):
        if row and _ILLUSTRATION.match(re.sub(r"-\s*", "", (row[0] or "").strip())):
            return True
    return False


# ---------------------------------------------------------------- value normalisers

_EMPTY_MARKS = {"-", "–", "—", "none", "n/a", "tbd"}


def is_empty_mark(v: str) -> bool:
    """True for placeholder cells meaning 'no value'."""
    return (v or "").strip().lower() in _EMPTY_MARKS


def split_list(v: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,;\n]+", v or "") if p.strip() and not is_empty_mark(p)]


def norm_direction(v: str) -> str:
    s = _hn(v)
    if s in ("provided", "provide", "provides", "p", "p port", "pport", "provider", "out", "output", "server",
             "provided port", "pp"):
        return "provided"
    if s in ("required", "require", "requires", "r", "r port", "rport", "requirer", "in", "input", "client",
             "required port", "rp"):
        return "required"
    return "unknown"


def norm_kind(v: str) -> str:
    s = _hn(v).replace(" ", "")
    if s in ("senderreceiver", "sr", "senderreceiverinterface", "sender/receiver", "s/r", "srinterface", "srif"):
        return "sender_receiver"
    if s in ("clientserver", "cs", "clientserverinterface", "client/server", "c/s", "csinterface", "csif"):
        return "client_server"
    return "unknown"


def _cell(row: list[str], cm: dict[str, int], key: str, keep_newlines: bool = False) -> str:
    i = cm.get(key)
    if i is None or i >= len(row) or is_empty_mark(row[i] or ""):
        return ""
    if keep_newlines:
        return (row[i] or "").strip()
    return re.sub(r"\s+", " ", (row[i] or "")).strip()


def _align(table: dict) -> dict[str, int]:
    return classify_table(table)[1]


# ---------------------------------------------------------------- table -> entities

def entities_from_table(table: dict, source: Source) -> list[_Entity]:
    if is_naming_illustration(table):
        return []
    source = source.model_copy(update={"method": "table"})
    kind, cm = classify_table(table)
    rows = table.get("rows", [])
    header = table.get("header") or []
    out: list[_Entity] = []
    if kind is None:
        return _key_value_interface(table, source)
    # handle rows with filler cells removed (shorter than header)
    nonempty = [i for i, h in enumerate(header) if (h or "").strip()]
    if len(nonempty) < len(header):
        remap = {i: k for k, i in enumerate(nonempty)}
        cm_row = {k: remap[v] for k, v in cm.items() if v in remap}
    else:
        cm_row = cm
    for row_index, row in enumerate(rows):
        r = row
        row_pages = table.get("row_pages", [])
        source = source.model_copy(update={"evidence": " | ".join(str(x or "") for x in row),
            "page": row_pages[row_index] if row_index < len(row_pages) else source.page})
        cmx = cm if len(row) == len(header) else cm_row
        if kind == "component":
            name = _cell(r, cmx, "component")
            if not name:
                continue
            out.append(Component(name=name, description=_cell(r, cmx, "description"),
                                 category=_cell(r, cmx, "category"), sources=[source]))
        elif kind == "port":
            name = _cell(r, cmx, "port")
            if not name:
                continue
            out.append(Port(name=name, component=_cell(r, cmx, "component"),
                            direction=norm_direction(_cell(r, cmx, "direction")),
                            interface=_cell(r, cmx, "interface"),
                            kind=norm_kind(_cell(r, cmx, "kind")) if "direction" in cmx else "unknown",
                            description=_cell(r, cmx, "description"), sources=[source]))
        elif kind == "interface":
            name = _cell(r, cmx, "interface")
            if not name:
                continue
            out.append(Interface(name=name, kind=norm_kind(_cell(r, cmx, "kind")),
                                 data_elements=split_list(_cell(r, cmx, "data_elements", True)),
                                 description=_cell(r, cmx, "description"), sources=[source]))
        elif kind == "signal":
            name = _cell(r, cmx, "signal")
            if not name:
                continue
            out.append(Signal(name=name, sender=_cell(r, cmx, "sender"),
                              receivers=split_list(_cell(r, cmx, "receivers", True)),
                              data_type=_cell(r, cmx, "data_type"),
                              description=_cell(r, cmx, "description"), sources=[source]))
    return out


def _key_value_interface(table: dict, source: Source) -> list[_Entity]:
    """HMI 2-col interface tables: row0 = single-cell name, then Description / Comments rows."""
    if table.get("kind") != "key_value":
        return []
    rows = [r for r in table.get("rows", []) if r]
    if len(rows) < 2 or len([c for c in rows[0] if (c or "").strip()]) != 1:
        return []
    labels = {(r[0] or "").strip().lower(): " ".join(r[1:]).strip() for r in rows[1:] if len(r) >= 2}
    if "description" not in labels:
        return []
    name = rows[0][0].strip()
    desc = re.sub(r"\s+", " ", labels["description"])
    itf = Interface(name=name, kind="unknown", description=desc, sources=[source])
    return [itf]


# ---------------------------------------------------------------- heuristics (prose / headings)

COMPONENT_CHAPTER = re.compile(r"software compositions?|basic architecture|\bcomponents\b", re.I)
_STOP_TITLES = re.compile(
    r"known (deficienc|defect)|design rationale|personali[sz]ation|^architectures?$|configurations?$|"
    r"decomposition|issues|explanation of|^list of|^example|inter-?\s?domain\s*interfaces|^actuators$|"
    r"additional information|limitations|appendix|glossary|references|introduction|purpose", re.I)
_SENTENCE = re.compile(
    r"\b([A-Z][A-Za-z]+(?:[A-Z][a-z]+)*)\s+(?:is|are)\s+an?\s+(actuator|sensor|adaptation|adapter)\s+component")
_FUNCTIONAL_COMPONENT = re.compile(
    r"(?:^|(?<=[.!?])\s+)([A-Z][A-Za-z0-9_]+)\s+"
    r"(?:senses|controls|manages|drives|provides\s+the\s+core\s+functionality)\b[^.!?]*(?:[.!?]|$)")
_BULLET = re.compile(r"[¾•]\s*([A-Z][A-Za-z]{1,10})\s*:\s*([A-Z][A-Za-z ]+?)(?=\s*[¾•]|$)")
_LABEL = re.compile(r"\b((?:Cmd|Req|Sta|Dis|Err|Hmi)[A-Z]\w+)\s*/\s*(command|request|status|display|value|failure)\b")
_PREFIX_MEANING = {"Cmd": "command (master to actuator)", "Req": "request (sensor to master)",
                   "Sta": "status", "Dis": "display feedback", "Err": "failure feedback (actuator to master)",
                   "Hmi": "user request (HMI)"}


def clean_text(t: str) -> str:
    t = re.sub(r"-\s*\n\s*(?=[a-z])", "", t or "")
    return re.sub(r"\s+", " ", t)


def component_name_from_title(title: str) -> tuple[str, str]:
    """Return (name, description) derived from a chapter-4 style heading."""
    t = re.sub(r"\s+", " ", title).strip()
    t = re.sub(r"^Software Compositions?\s+", "", t, flags=re.I)
    m = re.match(r"^([A-Z]{2,8})\s*\((.+)\)$", t)
    if m:
        return m.group(1), m.group(2).strip()
    m = re.match(r"^(.+?)\s*\(([^)]*)\)$", t)
    if m and len(t) < 40:
        return m.group(1).strip(), m.group(2).strip()
    return t, ""


_GENERIC_TITLE = re.compile(r"^(?:\d+(?:\.\d+)*\s+)?(overview|description|architecture|general|summary|scope|concept|"
                            r"design|structure|functionality|introduction|details?)$", re.I)
MIN_OWN_PROSE = 40  # characters of own prose below which a heading is only a label


def _heading_candidate(s: dict) -> bool:
    """A chapter-4/5 style heading that could name a component (before the container / no-prose rule)."""
    is_kv = any(t.get("kind") == "key_value" for t in s.get("tables", []))
    return "." in s["section_id"] and not is_kv and not _STOP_TITLES.search(s["title"])


def is_container_heading(s: dict, sections: list[dict]) -> bool:
    """C4-noise rule: a heading is NOT a component when it is a generic title (Overview, Description, ...), a
    container whose child headings are themselves component candidates (4.3 Interior Light > 4.3.1 ..), or has no
    prose of its own and no tables. Such headings group components; they are not components."""
    if _GENERIC_TITLE.match(s["title"].strip()):
        return True
    sid = s["section_id"]
    if any(c.get("parent_id") == sid and _heading_candidate(c) for c in sections):
        return True
    return len(clean_text(s.get("text", ""))) < MIN_OWN_PROSE and not s.get("tables")


def heuristic_entities(doc: dict) -> dict[str, list[_Entity]]:
    doc_id, version = doc["doc_id"], doc.get("version")
    sections = doc["sections"]
    title_of = {s["section_id"]: s["title"] for s in sections}
    comps: list[_Entity] = []
    sigs: list[_Entity] = []
    for s in sections:
        sid = s["section_id"]
        root = sid.split(".")[0]
        if sid == "0" or not COMPONENT_CHAPTER.search(title_of.get(root, "")):
            continue
        src = Source(doc_id=doc_id, version=version, page=s["page_start"], section=f"{sid} {s['title']}", method="heuristic")
        text = clean_text(s.get("text", ""))
        is_kv_interface = any(t.get("kind") == "key_value" for t in s.get("tables", []))
        if _heading_candidate(s) and not is_container_heading(s, sections):
            name, desc = component_name_from_title(s["title"])
            comps.append(Component(name=name, description=desc, sources=[src], extraction_method="heuristic", confidence="Medium"))
        for m in _SENTENCE.finditer(text):
            comps.append(Component(name=m.group(1), category=m.group(2), sources=[src],
                                   description=text[max(0, m.start()): m.end()].strip(),
                                   extraction_method="heuristic", confidence="Medium"))
        for match in _FUNCTIONAL_COMPONENT.finditer(text):
            if not _comp_ok(match.group(1)):
                continue
            evidence = match.group(0).lstrip(".!? ").strip()
            pages = [int(page) for page, page_text in s.get("page_texts", {}).items()
                     if _norm_text(evidence) in _norm_text(page_text)]
            evidence_source = src.model_copy(update={"evidence": evidence,
                "page": pages[0] if len(pages) == 1 else src.page})
            comps.append(Component(name=match.group(1), description=evidence, sources=[evidence_source],
                                   extraction_method="heuristic", confidence="Medium"))
        if "." not in sid:
            for m in _BULLET.finditer(text):
                comps.append(Component(name=m.group(1), description=m.group(2).strip(), sources=[src],
                                       extraction_method="heuristic", confidence="Medium"))
        for m in _LABEL.finditer(text):
            pref = re.match(r"[A-Z][a-z]+", m.group(1)).group(0)
            sigs.append(Signal(name=m.group(1), data_type=m.group(2), sources=[src],
                               description=f"Diagram label '{m.group(1)} / {m.group(2)}': {_PREFIX_MEANING[pref]}",
                               extraction_method="heuristic", confidence="Medium"))
    return {"components": comps, "signals": sigs, "ports": prose_ports(doc), "interfaces": prose_interfaces(doc)}


# ---- prose port rules: "<Comp> offers the port <P> via the interface <I>" and friends (sentence-grounded)
_ID = r"[A-Za-z][A-Za-z0-9_]*"
_PROV_V = r"(?:offers?|provides?|exposes?|publishes|sends?|supplies|delivers?|exports?|serves?|owns?|has)"
_REQ_V = r"(?:requires?|consumes?|receives?|needs?|reads?|imports?|subscribes\s+to|calls?|uses?)"
_VIA = r"(?:via|through|using|over|with|typed\s+by|of\s+type|implements)"
_SUBJ_STOP = {"the", "it", "this", "that", "these", "those", "each", "every", "a", "an", "and", "which", "also",
              "component", "software", "swc", "port", "ports"}
# (1) verb ... port P [via the interface I]; subject = "<C>:" label or "<C> <verb>" earlier in the sentence
_PORT_CLAUSE = re.compile(
    rf"\b(?P<v>{_PROV_V}|{_REQ_V})\b(?P<mid>[^.;:]{{0,40}}?)\b(?:(?P<pd>provided|required)\s+)?port\s+"
    rf"(?P<p>{_ID})(?:\s*[,(]?\s*{_VIA}\s+(?:the\s+)?(?:(?:port\s+)?interface\s+)?(?P<i>{_ID}))?", re.I)
# (2) the required/provided port P of C uses / is typed by I
_PORT_OF = re.compile(
    rf"\b(?:the\s+)?(?P<d>provided|required)\s+port\s+(?P<p>{_ID})\s+(?:of|on|at)\s+(?:the\s+)?(?:component\s+)?"
    rf"(?P<c>{_ID})\s+(?:uses|is\s+typed\s+by|is\s+typed\s+with|implements|is\s+of\s+type|has\s+the\s+interface|"
    rf"carries|exposes)\s+(?:the\s+)?(?:interface\s+)?(?P<i>{_ID})", re.I)
# (3) <C> provides/requires <PortLikeName> via the interface <I>   (name without the word 'port': Pp/Rp prefix only)
_VERB_PORTNAME = re.compile(
    rf"\b(?P<c>{_ID})\s+(?P<v>{_PROV_V}|{_REQ_V})\s+(?:the\s+)?(?P<p>(?:Pp|Rp)_?[A-Z][A-Za-z0-9_]*)\s+{_VIA}\s+"
    rf"(?:the\s+)?(?:interface\s+)?(?P<i>{_ID})")
_SUBJ = re.compile(rf"(?:^|(?<=[.!?]\s))(?:the\s+)?(?:(?:software\s+)?component\s+|swc\s+)?(?P<c>{_ID})\s*(?::\s*|\s+(?=(?:{_PROV_V}|{_REQ_V})\b))", re.I)
_PORT_STOP = {"interface", "interfaces", "name", "names", "prototype", "which", "that", "and", "type", "types",
              "list", "table", "number", "direction", "ports", "of", "to", "for", "in"}


def _ident_ok(s: str) -> bool:
    """CamelCase / prefixed identifier (not a plain English word)."""
    return bool(s) and s.lower() not in _PORT_STOP and bool(re.search(r"[A-Z0-9_]", s[1:])) and len(s) >= 3


def _comp_ok(s: str) -> bool:
    return s.lower() not in _SUBJ_STOP and (bool(re.search(r"[A-Z0-9_]", s[1:])) and s[0].isupper()) and len(s) >= 3


def _prose_dir(verb: str, pd: str, name: str) -> str:
    if pd:
        return pd.lower()
    if re.match(_REQ_V, verb, re.I) and not re.match(_PROV_V, verb, re.I):
        d = "required"
    elif re.match(_PROV_V, verb, re.I):
        d = "provided"
    else:
        d = "unknown"
    if name[:2] in ("Pp", "PP"):
        return "provided" if d in ("provided", "unknown") else d
    if name[:2] in ("Rp", "RP"):
        return "required" if d in ("required", "unknown") else d
    return d


def prose_ports(doc: dict) -> list[_Entity]:
    """Deterministic port extraction from running text (method 'heuristic', Medium): every port is grounded in one
    sentence that names the component, the port, a direction cue (verb / provided|required) and usually the
    interface. Conservative: the port name must look like an identifier and 'of C' / 'C:' must name a component."""
    doc_id, version = doc["doc_id"], doc.get("version")
    out: list[_Entity] = []
    for s in doc["sections"]:
        text = clean_text(s.get("text", ""))
        if not text or not re.search(r"\bports?\b|\bPp|\bRp", text):
            continue
        src = Source(doc_id=doc_id, version=version, page=s["page_start"], section=f"{s['section_id']} {s['title']}",
                     method="heuristic")
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text):
            def add(c: str, p: str, d: str, i: str, ev: str) -> None:
                if not (_comp_ok(c) and _ident_ok(p)) or (i and not _ident_ok(i)):
                    return
                out.append(Port(name=p, component=c, direction=d, interface=i if i and i.lower() not in _PORT_STOP else "",
                                sources=[src.model_copy(update={"evidence": ev[:200]})],
                                extraction_method="heuristic", confidence="Medium"))
            for m in _PORT_OF.finditer(sent):
                add(m.group("c"), m.group("p"), m.group("d").lower(), m.group("i"), m.group(0))
            for m in _VERB_PORTNAME.finditer(sent):
                add(m.group("c"), m.group("p"), _prose_dir(m.group("v"), "", m.group("p")), m.group("i"), m.group(0))
            sm = _SUBJ.search(sent)
            if not sm:
                continue
            for m in _PORT_CLAUSE.finditer(sent, sm.end()):
                add(sm.group("c"), m.group("p"), _prose_dir(m.group("v"), m.group("pd") or "", m.group("p")),
                    m.group("i") or "", m.group(0))
    return out


# ---- prose interfaces: "the interface DiagnosticMonitor", "the interfaces DiagSensor, DiagBrightness",
#      "virtual interior light interfaces: IntLightGroup0, IntLightGroup1, ..."  (written-out names only)
_ID_LIST = rf"{_ID}(?:\s*(?:,|\band\b|\bor\b)\s*{_ID})*"
_ITF_NAMED = re.compile(rf"\b(?:port\s+)?interfaces?\s+(?:named\s+|called\s+)?(?P<l>{_ID_LIST})")
_ITF_COLON = re.compile(rf"\binterfaces?\b[^.:;]{{0,80}}:\s*(?P<l>{_ID}(?:\s*(?:,|…|\.\.\.)\s*(?:…\s*)?{_ID})*)")


def prose_interfaces(doc: dict) -> list[_Entity]:
    """Interfaces named in running text (method 'heuristic', Medium): identifier-like names written right after
    'interface(s)' or listed after 'interfaces ...:'. A range written with '…' is not expanded; a name broken over
    a line ('Diag- Brightness') is joined."""
    doc_id, version = doc["doc_id"], doc.get("version")
    out: list[_Entity] = []
    for s in doc["sections"]:
        text = re.sub(r"(?<=[a-z])-\s+(?=[A-Z])", "", clean_text(s.get("text", "")))
        if not re.search(r"\binterfaces?\b", text):
            continue
        src = Source(doc_id=doc_id, version=version, page=s["page_start"], section=f"{s['section_id']} {s['title']}",
                     method="heuristic")
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text):
            names: list[str] = []
            for rx in (_ITF_NAMED, _ITF_COLON):
                for m in rx.finditer(sent):
                    names += [n for n in re.findall(_ID, m.group("l")) if n not in ("and", "or")]
            for n in dict.fromkeys(names):  # interface identifiers are CamelCase: a bare acronym (ACC) is not one
                if _ident_ok(n) and n[0].isupper() and re.search(r"[a-z]", n):
                    out.append(Interface(name=n, description=f"Interface named in the text of section {s['section_id']}",
                                         sources=[src.model_copy(update={"evidence": sent[:200]})],
                                         extraction_method="heuristic", confidence="Medium"))
    return out


# ---- software-composition groups: "The WiperWasher software component group controls ...",
#      "The SW-Composition ACC ...", "ESC SW-Composition"
_GROUP_KIND = r"(?:software\s+component\s+group|SW-?\s?[Cc]omposition|software\s+composition)"
_GROUP = (re.compile(rf"\b(?P<a>[A-Z][A-Za-z0-9]+)\s+{_GROUP_KIND}\b"),   # 'ESC SW-Composition' (name before)
          re.compile(rf"\b{_GROUP_KIND}\s+(?P<b>[A-Z][A-Za-z0-9]+)\b"))   # 'The SW-Composition ACC' (name after)
_GROUP_STOP = {"The", "This", "That", "These", "Each", "Every", "An", "A", "Other", "Its", "Their", "Following", "AUTOSAR"}


def composition_groups(doc: dict, components: list[_Entity]) -> list[_Entity]:
    """Group components (category 'composition') declared by a 'software component group' / 'SW-Composition'
    sentence. Children are the components extracted from the group's own section (the section whose title names
    the group, e.g. '4.1 Wiper/Washer' for WiperWasher) and its subsections; a group mentioned only in passing
    gets no children."""
    doc_id, version = doc["doc_id"], doc.get("version")
    sections = [s for s in doc["sections"] if s["section_id"] != "0"]
    titles = {normalize_name(component_name_from_title(s["title"])[0]) for s in sections}
    found: dict[str, dict] = {}
    for s in sections:
        text = clean_text(s.get("text", ""))
        for m in (m for rx in _GROUP for m in rx.finditer(text)):
            before = "a" in m.groupdict()
            name = m.group("a") if before else m.group("b")
            if name in _GROUP_STOP or len(name) < 2:
                continue
            if before and re.search(r"\b[A-Z][A-Za-z0-9]*\s+$", text[:m.start()]) \
                    and not re.search(r"\b(?:The|the|This|this|An?|an?)\s+$", text[:m.start()]):
                continue  # last word of a multi-word name ('... Driver Assistance System SW-Composition')
            named = (re.fullmatch(r"[A-Z]{2,6}", name) or re.search(r"[a-z][A-Z]", name)
                     or normalize_name(name) in titles)
            if named:
                found.setdefault(name, s)
    out: list[_Entity] = []
    for name, first in found.items():
        key = normalize_name(name)
        home = next((s for s in sections if normalize_name(component_name_from_title(s["title"])[0]) == key), None)
        sec = home or first
        sid = sec["section_id"]
        children: list[str] = []
        if home:
            for c in components:
                if component_key(c.name) == component_key(name) or _COMPOUND.search(c.name) \
                        or _TYPE_ANNOTATION.search(c.name) or is_junk_name(c.name):
                    continue  # the same precision rules refine_entities applies to components
                secs = [src.section.split(" ", 1)[0] for src in c.sources if src.section]
                if any(x == sid or x.startswith(sid + ".") for x in secs) and c.name not in children:
                    children.append(c.name)
        desc = ("Software composition containing " + ", ".join(children)) if children else "Software composition"
        out.append(Component(name=name, category="composition", children=children, description=desc,
                             sources=[Source(doc_id=doc_id, version=version, page=sec["page_start"],
                                             section=f"{sid} {sec['title']}", method="heuristic")],
                             extraction_method="heuristic", confidence="Medium"))
    return out


class LLMItem(BaseModel):
    name: str
    description: str = Field(default="", max_length=120)
    evidence: str = Field(min_length=3, max_length=500)


class LLMPort(BaseModel):
    name: str
    component: str = ""
    direction: Literal["provided", "required", "unknown"] = "unknown"
    interface: str = ""
    evidence: str = Field(min_length=3, max_length=500)


class LLMInterface(BaseModel):
    name: str
    kind: Literal["sender_receiver", "client_server", "unknown"] = "unknown"
    evidence: str = Field(min_length=3, max_length=500)


class LLMSignal(BaseModel):
    name: str
    sender: str = ""
    receivers: list[str] = Field(default_factory=list)
    evidence: str = Field(min_length=3, max_length=500)


class ProseExtraction(BaseModel):
    """Schema the LLM must fill for one section chunk. Every item needs a verbatim evidence quote."""
    components: list[LLMItem] = Field(default_factory=list)
    ports: list[LLMPort] = Field(default_factory=list)
    interfaces: list[LLMInterface] = Field(default_factory=list)
    signals: list[LLMSignal] = Field(default_factory=list)


PROSE_SYSTEM = (
    "You extract AUTOSAR software architecture entities from ONE section of a High Level Design document.\n"
    "Return JSON with lists: components (software components / compositions / actuators / sensors), "
    "ports (name, component = the owner, direction = provided if the component offers/provides/sends it, "
    "required if it requires/consumes/receives it, interface = its interface name), interfaces (name, kind), "
    "signals (name, sender component, receivers = list of components).\n"
    "Rules: list ONLY entities that the text explicitly names. Never invent names. Use the exact spelling of the text. "
    "Use compact JSON and omit empty optional fields. Descriptions must be brief, at most 12 words. "
    "Every item's evidence field MUST contain the shortest supporting quote, at most 15 words copied word for word from the "
    "text, including that item's exact name. If you cannot provide that quote, omit the item. "
    "If the text names no such entity, return empty lists. Do not list generic words like 'system' or 'driver'."
)
_FRONT_TITLES = re.compile(r"front matter|table of contents|^contents$|list of (figures|tables)|revision history|"
                           r"document (change|information)|disclaimer|legal|acknowledg|bibliography|references$", re.I)
_GENERIC = {"system", "software", "component", "components", "interface", "interfaces", "driver", "autosar", "hld",
            "ecu", "swc", "port", "ports", "signal", "signals", "figure", "table", "the", "domain", "application",
            "diagram", "document", "release", "layer", "function", "functions", "information", "data", "user"}


def _norm_text(t: str) -> str:
    t = re.sub(r"-\s*\n\s*", "", t or "")           # rejoin hyphenated line breaks
    t = re.sub(r"[\s ]+", " ", t)
    return t.lower().strip()


def _name_ok(name: str) -> bool:
    n = (name or "").strip()
    if not (2 <= len(n) <= 60) or n.lower() in _GENERIC:
        return False
    toks = n.split()
    if len(toks) > 6 or re.search(r"\bRev\b|\bR\d+\.\d+|\bV\d+\.\d", n):
        return False
    if len({tk.lower() for tk in toks}) < len(toks) or re.search(r"SW[- ]?C\b|[\[\]]", n):
        return False  # figure-text artefacts: repeated tokens, 'SW-C' captions, [instance] qualifiers
    return all(tk[0].isupper() or tk[0].isdigit() or tk[0] in "_" or any(c.isupper() for c in tk[1:]) for tk in toks)


def _quote_ok(quote: str, norm_text: str) -> str:
    """Return the (<= 25 word) quote if it occurs verbatim (case/whitespace-insensitive) in the section, else ''."""
    q = re.sub(r"\s+", " ", quote or "").strip().strip("\"'")
    if len(q) < 3:
        return ""
    words = q.split()
    if len(words) > 25:
        q = " ".join(words[:25])
    return q if _norm_text(q) in norm_text else ""


_PAGE_NOISE = re.compile(r"^(R\d+\.\d+( Rev \d+)?|V\d+\.\d+\.\d+|\d+ of \d+|Document ID .*|- ?AUTOSAR CONFIDENTIAL-?)$", re.I)


def prose_text(section: dict) -> str:
    """Section text without diagram-label noise: lines of <= 3 words without sentence punctuation are dropped
    (figure labels such as 'StaLightFault / status' or 'MirrorMove')."""
    keep = []
    for ln in (section.get("text", "") or "").splitlines():
        s = ln.strip()
        if not s:
            keep.append("")
            continue
        if _PAGE_NOISE.match(s):
            continue
        if len(s.split()) <= 3 and not re.search(r"[.:;,!?)]$", s) and not s.endswith("-"):
            continue
        keep.append(ln)
    return "\n".join(keep)


def llm_section_skippable(section: dict) -> str:
    """Reason to skip a section (front matter, TOC, too short after removing figure labels), or ''."""
    if section.get("section_id") == "0" or _FRONT_TITLES.search(section.get("title", "")):
        return "front matter"
    if len(clean_text(prose_text(section))) < settings.llm_extract_min_chars:
        return "too short"
    return ""


def chunk_section_text(text: str, max_chars: int | None = None) -> list[str]:
    """Split section text on paragraph/sentence boundaries into chunks <= max_chars (keeps the 4096 ctx safe)."""
    max_chars = max_chars or settings.llm_extract_chunk_chars
    text = re.sub(r"-\s*\n\s*(?=[a-z])", "", text or "")
    parts = [p.strip() for p in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in parts:
        while len(p) > max_chars:  # pathological sentence: hard split
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:max_chars])
            p = p[max_chars:]
        if len(cur) + len(p) + 1 > max_chars and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def extract_from_prose(section: dict, doc_id: str = "", version: str | None = None, provider=None,
                       model: str | None = None, stats: dict | None = None) -> list[_Entity]:
    """LLM extraction from one section (chunked to fit num_ctx). Entities whose evidence quote is not found
    verbatim in the section text are discarded (anti-hallucination). Returns [] without a provider."""
    if llm_section_skippable(section):
        return []
    try:
        provider = provider or llm.get_provider()
    except ValueError:
        return []
    model = model or settings.llm_extract_model or None
    sec_text = prose_text(section)
    norm = _norm_text(sec_text)
    sid = f"{section.get('section_id', '')} {section.get('title', '')}".strip()
    out: list[_Entity] = []
    for ci, chunk in enumerate(chunk_section_text(sec_text)):
        prompt = f"Section: {section.get('title', '')}\n\nText:\n{chunk}"
        try:
            data = provider.generate(prompt, PROSE_SYSTEM, schema=ProseExtraction, model=model,
                                     purpose="extract_prose", chunk_ids=[f"{doc_id}:{section.get('section_id', '')}:{ci}"])
        except llm.LLMNotConfigured:
            return []
        except ValueError:  # schema validation failed after retries: skip this chunk
            if stats is not None:
                stats["failed_chunks"] = stats.get("failed_chunks", 0) + 1
            continue
        if not isinstance(data, dict):
            continue
        for kind, cls, key in (("components", Component, "components"), ("ports", Port, "ports"),
                               ("interfaces", Interface, "interfaces"), ("signals", Signal, "signals")):
            for d in data.get(key, []) or []:
                nm = (d.get("name") or "").strip()
                quote = _quote_ok(d.get("evidence", ""), _norm_text(chunk))
                if not _name_ok(nm) or not quote or not re.search(
                    r"(?<!\w)" + re.escape(_norm_text(nm)) + r"(?!\w)", _norm_text(quote)):
                    if stats is not None:
                        stats["dropped"] = stats.get("dropped", 0) + 1
                    continue
                pages = [int(p) for p, text in section.get("page_texts", {}).items()
                         if _norm_text(quote) in _norm_text(text)]
                page = pages[0] if len(pages) == 1 else (
                    section.get("page_start") if section.get("page_start") == section.get("page_end") else None)
                src = Source(doc_id=doc_id, version=version, page=page, section=sid,
                             method="llm", evidence=quote)
                kw: dict[str, Any] = {"name": nm, "sources": [src], "extraction_method": "llm", "confidence": "Medium"}
                if kind == "components":
                    description = (d.get("description") or "").strip()
                    kw["description"] = description if description and _norm_text(description) in _norm_text(quote) else quote
                elif kind == "ports":
                    g = lambda x: x if x and _norm_text(x) in _norm_text(quote) else ""  # noqa: E731
                    direction = d.get("direction", "unknown")
                    cues = {"provided": r"\b(provid\w*|offers?|outputs?|sends?|exposes?)\b",
                            "required": r"\b(requir\w*|needs?|consumes?|inputs?|receives?|reads?)\b"}
                    if direction in cues and not re.search(cues[direction], quote, re.I):
                        direction = "unknown"
                    kw.update(component=g((d.get("component") or "").strip()), direction=direction,
                              interface=g((d.get("interface") or "").strip()))
                elif kind == "interfaces":
                    proposed = d.get("kind", "unknown")
                    cue = {"sender_receiver": r"sender[\s_/-]*receiver|\bS/R\b",
                           "client_server": r"client[\s_/-]*server|\bC/S\b"}.get(proposed)
                    kw["kind"] = proposed if cue and re.search(cue, quote, re.I) else "unknown"
                else:
                    g = lambda x: x if x and _norm_text(x) in _norm_text(quote) else ""  # noqa: E731
                    kw.update(sender=g((d.get("sender") or "").strip()),
                              receivers=[r.strip() for r in d.get("receivers", []) if g((r or "").strip())])
                out.append(cls(**kw))
                if stats is not None:
                    stats["kept"] = stats.get("kept", 0) + 1
    return out


# ---------------------------------------------------------------- convention inference (C2)

PREFIX_ROLES = {  # prefix -> (sender role, receiver role, confidence)
    "Cmd": ("master", "actuator", "Medium"),
    "Req": ("sensor", "master", "Medium"),
    "Sta": ("actuator", "master", "Low"),
    "Err": ("actuator", "master", "Medium"),
    "Dis": ("master", "display", "Low"),
    "Hmi": ("HMI", "master", "Low"),
}
_PREFIX_RE = re.compile(r"^(Cmd|Req|Sta|Dis|Err|Hmi)(?=[A-Z])")


def split_prefix(name: str) -> tuple[str, str]:
    m = _PREFIX_RE.match(name or "")
    return (m.group(1), name[m.end():]) if m else ("", name or "")


def infer_by_convention(found: dict[str, list[_Entity]], doc_id: str, version: str | None) -> dict[str, list[_Entity]]:
    """Naming-convention layer (never overrides other evidence; merge ranks it last, confidence <= Medium):
    - signal prefix -> sender_role / receiver_role (Cmd master->actuator, Req sensor->master, Sta/Err actuator->master,
      Dis master->display, Hmi HMI->master)
    - a port of unknown direction whose name matches <Prefix><PortName> of a signal gets provided/required from that
      signal's sender/receivers
    Deliberately NOT done: inventing ports from signal names (real port names differ from <signal minus prefix>)."""
    out: dict[str, list[_Entity]] = {"signals": [], "ports": []}
    sig_by_port: dict[str, list[Signal]] = {}
    for s in found.get("signals", []):
        pref, rest = split_prefix(s.name)
        if not pref:
            continue
        sig_by_port.setdefault(normalize_name(rest), []).append(s)  # type: ignore[arg-type]
        snd, rcv, conf = PREFIX_ROLES[pref]
        src = s.sources[0] if s.sources else Source(doc_id=doc_id, version=version)  # best-ranked source first
        csrc = src.model_copy(update={"method": "convention", "evidence": ""})
        out["signals"].append(Signal(
            name=s.name, sender_role=snd, receiver_role=rcv, sources=[csrc], extraction_method="convention",
            confidence=conf, description=f"Name prefix '{pref}': {PREFIX_ROLES[pref][0]} -> {PREFIX_ROLES[pref][1]} "
                                         "(inferred from the naming convention)"))
    for p in found.get("ports", []):
        if p.direction != "unknown" or not p.component:
            continue
        for s in sig_by_port.get(p.norm_name, []):
            c = normalize_name(p.component)
            d = ("provided" if normalize_name(s.sender) == c else
                 "required" if c in {normalize_name(r) for r in s.receivers} else "")
            if d:
                csrc = (p.sources[0] if p.sources else Source(doc_id=doc_id, version=version)).model_copy(
                    update={"method": "convention", "evidence": ""})
                out["ports"].append(Port(name=p.name, component=p.component, direction=d, sources=[csrc],
                                         extraction_method="convention", confidence="Medium",
                                         description=f"Direction inferred from signal '{s.name}' sender/receivers"))
                break
    return out


# ---------------------------------------------------------------- merge

def _key(e: _Entity) -> tuple:
    if isinstance(e, Port):
        return ("port", normalize_name(e.component), e.norm_name)
    return (e.entity_type, e.norm_name)


def merge_entities(items: list[_Entity]) -> list[_Entity]:
    """Merge duplicates (same type + normalised name); keep every source (each with its method and page).
    Precedence table > diagram > heuristic > llm > convention: a lower-ranked source only fills empty/unknown fields.
    Confidence = that of the best-ranked source; two or more distinct agreeing methods raise it one step
    (Low->Medium, Medium->High) but a convention-only/convention-agreeing pair never exceeds Medium."""
    merged: dict[tuple, _Entity] = {}
    methods: dict[tuple, set] = {}
    for e in items:
        k = _key(e)
        methods.setdefault(k, set()).add(e.extraction_method)
        cur = merged.get(k)
        if cur is None:
            merged[k] = e.model_copy(deep=True)
            continue
        for src in e.sources:
            if src not in cur.sources:
                cur.sources.append(src)
        better = _METHOD_RANK[e.extraction_method] > _METHOD_RANK[cur.extraction_method]
        if isinstance(e, Signal) and isinstance(cur, Signal) and e.sender and cur.sender and                 normalize_name(e.sender) != normalize_name(cur.sender) and not better and                 e.extraction_method == cur.extraction_method:
            if e.sender not in cur.extra_senders:
                cur.extra_senders.append(e.sender)   # another drawn arrow with the same label
        for fname, val in e.field_dict().items():
            old = getattr(cur, fname)
            if isinstance(old, list):
                for x in val:
                    if x not in old:
                        old.append(x)
            elif (not old or old == "unknown" or (better and val and val != "unknown")) and val:
                setattr(cur, fname, val)
        if better:
            cur.extraction_method = e.extraction_method
            cur.confidence = e.confidence
    for k, e in merged.items():
        # best evidence first (stable); a text-layer source always comes before an OCR-dependent one, so the
        # row's page / citation is a page that was read as text whenever there is one
        e.sources.sort(key=lambda s: (s.ocr, -_METHOD_RANK.get(s.method, 0)))
        if isinstance(e, Signal):
            if any(s.method == "diagram" for s in e.sources):  # exact drawn page supersedes section-start page
                e.sources = [s for s in e.sources if s.method != "heuristic"]
            sn = normalize_name(e.sender)
            e.receivers = [r for r in e.receivers if normalize_name(r) != sn]
            e.extra_senders = [x for x in e.extra_senders if normalize_name(x) != sn]
        ms = methods[k]
        if len(ms - {"convention"}) >= 2 and e.extraction_method != "convention" and e.confidence != "High":
            e.confidence = "High" if e.confidence == "Medium" else "Medium"
        if e.extraction_method == "convention" and e.confidence == "High":
            e.confidence = "Medium"
    return list(merged.values())


# ---------------------------------------------------------------- precision rules (generic, after merging)
_TYPE_ANNOTATION = re.compile(r"(?<!:):(?!:)")          # 'Name: EcucIntegerParamDef' (Inst::Type keeps '::')
_COMPOUND = re.compile(r"[,/&(]|\b(?:and|or|including|incl)\b", re.I)
MIN_QUOTE_EXTRA_WORDS = 3  # an llm quote must say something about the name, not only repeat it


def _quote_extra_words(e: _Entity) -> int:
    best = 0
    for s in e.sources:
        q = _norm_text(s.evidence)
        if s.method == "llm" and q:
            best = max(best, len(re.findall(r"[a-z0-9]+", q.replace(_norm_text(e.name), " ", 1))))
    return best


def component_key(name: str) -> str:
    """Near-duplicate key: 'RSC Roll Stability Control' -> 'rsc' (acronym + its expansion), plural 's' removed
    ('Exterior Lights' == 'Exterior Light'), case/spacing ignored ('Central Locking' == 'CentralLocking')."""
    toks = re.findall(r"[A-Za-z0-9]+", name or "")
    if len(toks) >= 3 and re.fullmatch(r"[A-Z]{2,6}", toks[0]):
        initials = "".join(t[0] for t in toks[1:] if t[0].isupper())
        if initials == toks[0]:
            return toks[0].lower()
    n = normalize_name(name)
    return n[:-1] if len(n) > 4 and n.endswith("s") and not n.endswith("ss") else n


def _display_rank(e: _Entity) -> tuple:
    camel = bool(re.search(r"[a-z][A-Z]", e.name)) and " " not in e.name.strip()
    return (_METHOD_RANK[e.extraction_method], camel, -len(e.name))


def refine_entities(ents: dict[str, list[_Entity]]) -> dict[str, list[_Entity]]:
    """Precision rules: drop 'Name: Type' annotations (all types), compound component names (headings/captions
    joining several elements with , / & ( and/or/incl), llm-only components whose verbatim quote is little more
    than the name, and merge near-duplicate components (plural, acronym + expansion, spacing) under the best-ranked
    name (method rank, then CamelCase, then the shorter name)."""
    out = {t: [e for e in v if not _TYPE_ANNOTATION.search(e.name)] for t, v in ents.items()}
    comps = []
    for e in out.get("components", []):
        if _COMPOUND.search(e.name):
            continue
        if e.extraction_method == "llm" and all(s.method == "llm" for s in e.sources) \
                and _quote_extra_words(e) < MIN_QUOTE_EXTRA_WORDS:
            continue
        comps.append(e)
    groups: dict[str, list[_Entity]] = {}
    for e in comps:
        groups.setdefault(component_key(e.name), []).append(e)
    merged = []
    for g in groups.values():
        if len(g) > 1:
            name = max(g, key=_display_rank).name
            g = [e.model_copy(update={"name": name}) for e in g]
        merged.extend(g)
    out["components"] = merge_entities(merged)
    return out


# ---------------------------------------------------------------- document extraction

def extract_entities(doc: dict, use_llm: bool = False, pdf_path=None, use_diagram: bool = True,
                     use_convention: bool = True, provider=None, model: str | None = None,
                     stats: dict | None = None) -> dict[str, list[_Entity]]:
    """Pure function: processed doc dict -> merged entities per type.

    Layers: table (High), diagram geometry when ``pdf_path`` is given (Medium/High), heading/regex heuristics
    (Medium), llm prose (Medium, ``use_llm``), naming convention (Low/Medium, ``use_convention``)."""
    doc_id, version = doc["doc_id"], doc.get("version")
    found: dict[str, list[_Entity]] = {"components": [], "ports": [], "interfaces": [], "signals": []}
    for s in doc["sections"]:
        for t in s.get("tables", []):
            src = Source(doc_id=doc_id, version=version, page=t.get("page_start") or s.get("page_start"),
                         section=f"{s['section_id']} {s['title']}")
            for e in entities_from_table(t, src):
                found[ENTITY_TABLES[e.entity_type]].append(e)
    h = heuristic_entities(doc)
    for k, v in h.items():
        found[k].extend(v)
    if use_diagram and pdf_path:
        from app import diagram
        for k, v in diagram.extract_diagram_entities(doc, pdf_path).items():
            for e in v:
                e.sources = [s.model_copy(update={"method": "diagram"}) for s in e.sources]
            found[k].extend(v)
    if use_llm:
        for s in doc["sections"]:
            for e in extract_from_prose(s, doc_id, version, provider=provider, model=model, stats=stats):
                found[ENTITY_TABLES[e.entity_type]].append(e)
    if use_convention:
        stage1 = {k: merge_entities(v) for k, v in found.items()}
        for k, v in infer_by_convention(stage1, doc_id, version).items():
            found[k].extend(v)
    merged = {k: merge_entities(v) for k, v in found.items()}
    merged["components"] = merge_entities(merged["components"] + composition_groups(doc, merged["components"]))
    return refine_entities(merged) if os.environ.get("EXTRACT_REFINE", "1") != "0" else merged


SCHEMA_TMPL = """
CREATE TABLE IF NOT EXISTS {t}(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, doc_id TEXT NOT NULL, version TEXT,
  name TEXT NOT NULL, norm_name TEXT NOT NULL, fields TEXT, page INTEGER, section TEXT,
  sources_json TEXT, extraction_method TEXT, confidence TEXT NOT NULL DEFAULT 'High',
  review_status TEXT NOT NULL DEFAULT 'unreviewed');
CREATE INDEX IF NOT EXISTS idx_{t}_doc ON {t}(project_id, doc_id);
"""


def ensure_schema(conn) -> None:
    for t in ENTITY_TABLES.values():
        conn.executescript(SCHEMA_TMPL.format(t=t))
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        if "confidence" not in cols:  # databases created before Phase C
            conn.execute(f"ALTER TABLE {t} ADD COLUMN confidence TEXT NOT NULL DEFAULT 'High'")
        if "overrides_json" not in cols:
            conn.execute(f"ALTER TABLE {t} ADD COLUMN overrides_json TEXT NOT NULL DEFAULT '{{}}'")
        if "extraction_present" not in cols:
            conn.execute(f"ALTER TABLE {t} ADD COLUMN extraction_present INTEGER NOT NULL DEFAULT 1")
        if "machine_key" not in cols:
            conn.execute(f"ALTER TABLE {t} ADD COLUMN machine_key TEXT")


def _row_key(t: str, name_norm: str, fields: dict) -> tuple:
    if t == "ports":
        return (name_norm, normalize_name(fields.get("component", "")))
    return (name_norm,)


def resolve_pdf(doc: dict) -> str | None:
    from app import diagram
    p = diagram.find_pdf(doc)
    return str(p) if p else None


def extract_document(project_id: str, doc_id: str, conn=None, processed_dir=None, use_llm: bool = False,
                     pdf_path=None, use_diagram: bool = True, use_convention: bool = True, provider=None,
                     model: str | None = None, stats: dict | None = None) -> dict[str, int]:
    """Extract + store entities for one document.

    Idempotent and review-preserving: existing rows are matched by (project_id, doc_id, type, normalised key;
    ports also by component) and UPDATED in place, so their id, review_status and review/comment history
    (``reviews`` table, keyed by row id) survive. New entities are inserted, stale *unreviewed* rows are deleted,
    stale rows that carry a human decision are kept. ``pdf_path`` defaults to the PDF located by sha256 in
    raw/synthetic dirs (diagram layer is skipped when no PDF is found)."""
    doc = load_processed(project_id, doc_id, processed_dir)
    if pdf_path is None and use_diagram:
        pdf_path = resolve_pdf(doc)
    ents = extract_entities(doc, use_llm=use_llm, pdf_path=pdf_path, use_diagram=use_diagram,
                            use_convention=use_convention, provider=provider, model=model, stats=stats)
    with controls._use(conn) as c:
        ensure_schema(c)
        counts: dict[str, int] = {}
        for t, items in ents.items():
            items = [e for e in items if not is_junk_name(e.name)]
            old: dict[tuple, Any] = {}
            for r in c.execute(f"SELECT * FROM {t} WHERE project_id=? AND doc_id=?",
                               (project_id, doc_id)):
                key = tuple(json.loads(r["machine_key"])) if r["machine_key"] else _row_key(
                    t, r["norm_name"], json.loads(r["fields"] or "{}"))
                old.setdefault(key, r)
            seen: set[int] = set()
            for e in items:
                p = e.sources[0] if e.sources else Source(doc_id=doc_id)
                vals = (doc.get("version"), e.name, e.norm_name, json.dumps(e.field_dict(), ensure_ascii=False),
                        p.page, p.section, json.dumps([s.model_dump() for s in e.sources], ensure_ascii=False),
                        e.extraction_method, e.confidence)
                k = _row_key(t, e.norm_name, e.field_dict())
                if k in old and old[k]["id"] not in seen:
                    previous = old[k]
                    rid = previous["id"]
                    seen.add(rid)
                    overrides = json.loads(previous["overrides_json"] or "{}")
                    fields = {**e.field_dict(), **overrides}
                    vals = (*vals[:3], json.dumps(fields, ensure_ascii=False), *vals[4:])
                    # An approval applies to the reviewed facts, not changed machine output.
                    status = previous["review_status"]
                    changed = fields != json.loads(previous["fields"] or "{}")
                    if status in ("approved", "corrected") and changed:
                        status = "unreviewed"
                    c.execute(f"UPDATE {t} SET version=?, name=?, norm_name=?, fields=?, page=?, section=?, "
                              "sources_json=?, extraction_method=?, confidence=?, review_status=?, extraction_present=1 WHERE id=?",
                              (*vals, status, rid))
                else:
                    c.execute(
                        f"INSERT INTO {t}(project_id, doc_id, version, name, norm_name, fields, page, section, "
                        "sources_json, extraction_method, confidence, review_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (project_id, doc_id, *vals, e.review_status))
            for k, r in old.items():
                if r["id"] not in seen and r["review_status"] == "unreviewed" and not json.loads(r["overrides_json"] or "{}"):
                    c.execute(f"DELETE FROM {t} WHERE id=?", (r["id"],))
                elif r["id"] not in seen:
                    c.execute(f"UPDATE {t} SET extraction_present=0 WHERE id=?", (r["id"],))
            counts[t] = c.execute(f"SELECT COUNT(*) FROM {t} WHERE project_id=? AND doc_id=?",
                                  (project_id, doc_id)).fetchone()[0]
        c.commit()
    return counts


def correct_entity(project_id: str, item_type: str, item_id: int, changes: dict, comment: str, user: str,
                   conn=None) -> dict:
    """Persist validated field overrides, with the original extraction identity and a review trail."""
    with controls._use(conn) as c:
        controls.require_role(user, project_id, "reviewer", c)
        ensure_schema(c)
        table = ENTITY_TABLES[item_type]
        row = c.execute(f"SELECT * FROM {table} WHERE id=? AND project_id=?", (item_id, project_id)).fetchone()
        if row is None:
            raise ValueError("entity not found in this project")
        before = json.loads(row["fields"] or "{}")
        allowed = set(MODELS[item_type](name=row["name"]).field_dict())
        if not changes or set(changes) - allowed:
            raise ValueError("supply one or more editable entity fields")
        candidate = MODELS[item_type].model_validate({"name": row["name"], **before, **changes})
        fields = candidate.field_dict()
        overrides = {**json.loads(row["overrides_json"] or "{}"), **{k: fields[k] for k in changes}}
        key = row["machine_key"] or json.dumps(_row_key(table, row["norm_name"], before))
        c.execute(f"UPDATE {table} SET fields=?, overrides_json=?, machine_key=?, review_status='corrected' WHERE id=?",
                  (json.dumps(fields), json.dumps(overrides), key, item_id))
        controls.review(item_type, item_id, "corrected", comment, user, project_id=project_id, conn=c)
        controls.log_action(user, "correct_entity", f"{project_id}:{item_type}:{item_id}",
                            {"before": before, "after": fields, "comment": comment}, c)
        return {"doc_id": row["doc_id"], "version": row["version"], "changes": overrides}


def method_counts(project_id: str, doc_id: str, conn=None) -> dict[str, dict[str, int]]:
    """{entity table: {extraction_method: n}} for the stored inventory of one document."""
    out: dict[str, dict[str, int]] = {}
    with controls._use(conn) as c:
        ensure_schema(c)
        for t in ENTITY_TABLES.values():
            out[t] = {r[0]: r[1] for r in c.execute(
                f"SELECT extraction_method, COUNT(*) FROM {t} WHERE project_id=? AND doc_id=? GROUP BY 1",
                (project_id, doc_id))}
    return out


def get_inventory(project_id: str, doc_id: str | None = None, version: str | None = None, conn=None,
                  include_rejected: bool = True) -> dict[str, list[dict]]:
    """Stored entities as dicts: id, name, norm_name, doc_id, version, page, section, sources (list),
    extraction_method, confidence, review_status plus the type-specific fields flattened in."""
    out: dict[str, list[dict]] = {}
    with controls._use(conn) as c:
        ensure_schema(c)
        for t in ENTITY_TABLES.values():
            q, args = f"SELECT * FROM {t} WHERE project_id=?", [project_id]
            if not include_rejected:
                q += " AND review_status != 'rejected' AND extraction_present=1"
            if doc_id:
                q += " AND doc_id=?"
                args.append(doc_id)
            if version:
                q += " AND version=?"
                args.append(version)
            rows = []
            for r in c.execute(q + " ORDER BY id", args):
                d = {k: r[k] for k in ("id", "project_id", "doc_id", "version", "name", "norm_name", "page",
                                       "section", "extraction_method", "confidence", "review_status", "extraction_present")}
                d["corrected_fields"] = list(json.loads(r["overrides_json"] or "{}"))
                d["sources"] = json.loads(r["sources_json"] or "[]")
                d.update(json.loads(r["fields"] or "{}"))
                rows.append(d)
            out[t] = rows
    return out
