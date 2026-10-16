"""Revision comparison: entity-level diff between two versions of documents in one project.

Design choices:
- Entities are keyed by normalized name (ports: normalized "Component.Port").
- Only meaningful fields are compared, after normalising case/whitespace and sorting lists, so
  pagination/citation-only differences never count as changes.
- Possible renames (removed+added pair with fuzzy score >= settings.fuzzy_threshold) stay listed under
  added/removed (so counts are never silently hidden) AND are also reported in possible_renames.
- Entities whose ONLY evidence is the LLM (extraction_method == "llm") are unstable across releases (sampling
  noise in prose extraction), so when such an entity exists on one side only it is listed under
  `llm_only_unmatched` instead of added/removed (never silently dropped, never counted as a release change).
- Output is always review_status="unreviewed".
"""
from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr
from rapidfuzz import fuzz

from app import controls
from app.config import settings
from app.extract import get_inventory, normalize_name

ENTITY_TYPES = ("components", "ports", "interfaces", "signals")
COMPARE_FIELDS: dict[str, tuple[str, ...]] = {
    "components": ("description", "category"),
    "ports": ("direction", "interface", "kind"),
    "interfaces": ("kind", "data_elements"),
    "signals": ("sender", "receivers", "data_type", "description"),
}
LIST_FIELDS = {"data_elements", "receivers"}


class Citation(BaseModel):
    doc_id: str
    version: str | None = None
    page: int | None = None
    section: str = ""


class Entry(BaseModel):
    key: str
    name: str
    citations: list[Citation] = Field(default_factory=list)  # added: in B; removed: in A; changed: A then B
    changes: dict[str, dict[str, Any]] = Field(default_factory=dict)  # changed only: {field: {old, new}}


class Rename(BaseModel):
    old: str
    new: str
    score: float


class TypeDiff(BaseModel):
    added: list[Entry] = Field(default_factory=list)
    removed: list[Entry] = Field(default_factory=list)
    changed: list[Entry] = Field(default_factory=list)
    possible_renames: list[Rename] = Field(default_factory=list)
    llm_only_unmatched: list[Entry] = Field(default_factory=list)  # LLM-only entity present on one side only


class Diff(BaseModel):
    project_id: str
    doc_a: str
    version_a: str | None
    doc_b: str
    version_b: str | None
    components: TypeDiff = Field(default_factory=TypeDiff)
    ports: TypeDiff = Field(default_factory=TypeDiff)
    interfaces: TypeDiff = Field(default_factory=TypeDiff)
    signals: TypeDiff = Field(default_factory=TypeDiff)
    revisions_a: list[dict] = Field(default_factory=list)
    revisions_b: list[dict] = Field(default_factory=list)
    summary: dict[str, dict[str, int]] = Field(default_factory=dict)
    llm_only_unmatched_total: int = 0   # LLM-only entities on one side only (not counted in summary)
    sections: "SectionDiff | None" = None   # text/section diff (None when processed JSONs are unavailable)
    review_status: str = "unreviewed"
    label: str = "AI-generated comparison - unreviewed"


# ------------------------------------------------------------------ helpers

def _norm_text(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def _norm_value(field: str, v: Any) -> Any:
    if field in LIST_FIELDS:
        items = v if isinstance(v, list) else re.split(r"[,;\n]", str(v or ""))
        return sorted({_norm_text(i) for i in items if _norm_text(i)})
    return _norm_text(v)


def _key(etype: str, e: dict) -> str:
    if etype == "ports":
        return f"{normalize_name(e.get('component') or '?')}.{normalize_name(e['name'])}"
    return normalize_name(e["name"])


def _display(etype: str, e: dict) -> str:
    return f"{e.get('component') or '?'}.{e['name']}" if etype == "ports" else e["name"]


def _cits(e: dict) -> list[Citation]:
    srcs = e.get("sources") or [{"doc_id": e.get("doc_id"), "version": e.get("version"),
                                 "page": e.get("page"), "section": e.get("section") or ""}]
    return [Citation(doc_id=s.get("doc_id") or e.get("doc_id"), version=s.get("version") or e.get("version"),
                     page=s.get("page"), section=s.get("section") or "") for s in srcs]


def _index(etype: str, rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(_key(etype, r), r)
    return out


def _doc(c, project_id: str, doc_id: str, version: str | None):
    row = c.execute("SELECT * FROM documents WHERE project_id=? AND doc_id=?", (project_id, doc_id)).fetchone()
    if row is None:
        raise ValueError(f"document {doc_id!r} does not belong to project {project_id!r}")
    if version is not None and row["version"] is not None and str(row["version"]) != str(version):
        raise ValueError(f"document {doc_id!r} has version {row['version']!r}, not {version!r}")
    return row


def _revisions(row) -> list[dict]:
    try:
        return json.loads(row["revisions_json"] or "[]")
    except (ValueError, TypeError):
        return []


def _diff_type(etype: str, a_rows: list[dict], b_rows: list[dict]) -> TypeDiff:
    a, b = _index(etype, a_rows), _index(etype, b_rows)
    td = TypeDiff()
    for k in sorted(b.keys() - a.keys()):
        ent = Entry(key=k, name=_display(etype, b[k]), citations=_cits(b[k]))
        (td.llm_only_unmatched if b[k].get("extraction_method") == "llm" else td.added).append(ent)
    for k in sorted(a.keys() - b.keys()):
        ent = Entry(key=k, name=_display(etype, a[k]), citations=_cits(a[k]))
        (td.llm_only_unmatched if a[k].get("extraction_method") == "llm" else td.removed).append(ent)
    for k in sorted(a.keys() & b.keys()):
        ch = {}
        for f in COMPARE_FIELDS[etype]:
            va, vb = _norm_value(f, a[k].get(f)), _norm_value(f, b[k].get(f))
            if va != vb:
                ch[f] = {"old": a[k].get(f), "new": b[k].get(f)}
        if ch:
            td.changed.append(Entry(key=k, name=_display(etype, b[k]), citations=_cits(a[k]) + _cits(b[k]),
                                    changes=ch))
    # possible renames: best fuzzy pairing between removed and added display names
    pairs = []
    for r in td.removed:
        for ad in td.added:
            s = fuzz.ratio(r.key, ad.key)
            if s >= settings.fuzzy_threshold:
                pairs.append((s, r.name, ad.name))
    used_o, used_n = set(), set()
    for s, o, n in sorted(pairs, reverse=True):
        if o in used_o or n in used_n:
            continue
        used_o.add(o)
        used_n.add(n)
        td.possible_renames.append(Rename(old=o, new=n, score=round(float(s), 1)))
    return td


# ------------------------------------------------------------------ public API

def compare(project_id: str, doc_a: str, ver_a: str | None, doc_b: str, ver_b: str | None, conn=None,
            processed_dir=None) -> Diff:
    """Diff the stored inventories of (doc_a, ver_a) -> (doc_b, ver_b). Both docs must be in project_id."""
    with controls._use(conn) as c:
        ra, rb = _doc(c, project_id, doc_a, ver_a), _doc(c, project_id, doc_b, ver_b)
        va = ver_a if ver_a is not None else ra["version"]
        vb = ver_b if ver_b is not None else rb["version"]
        inv_a = get_inventory(project_id, doc_a, va, conn=c, include_rejected=False)
        inv_b = get_inventory(project_id, doc_b, vb, conn=c, include_rejected=False)
        diff = Diff(project_id=project_id, doc_a=doc_a, version_a=va, doc_b=doc_b, version_b=vb,
                    revisions_a=_revisions(ra), revisions_b=_revisions(rb))
    for t in ENTITY_TYPES:
        setattr(diff, t, _diff_type(t, inv_a.get(t, []), inv_b.get(t, [])))
        td = getattr(diff, t)
        diff.summary[t] = {"added": len(td.added), "removed": len(td.removed), "changed": len(td.changed),
                           "possible_renames": len(td.possible_renames)}
    diff.summary["total"] = {k: sum(diff.summary[t][k] for t in ENTITY_TYPES)
                             for k in ("added", "removed", "changed", "possible_renames")}
    diff.llm_only_unmatched_total = sum(len(getattr(diff, t).llm_only_unmatched) for t in ENTITY_TYPES)
    diff.sections = _sections_for(project_id, doc_a, doc_b, processed_dir)
    return diff


# ------------------------------------------------------------------ section-level (text) diff
# Design: sections of two processed doc JSONs are aligned by section number + normalised title, then by title
# alone (renumbered), then by fuzzy title (retitled). Text is compared after stripping header/footer noise (page
# "N of M", "Document ID ...", confidentiality line, running release strings like "R3.2 Rev 1" - only that exact
# running-header form), de-hyphenating line breaks and ignoring whitespace/case/punctuation. Front matter
# (section "0") and change-history tables go to a separate `front_matter` bucket, never counted as body changes.
SEC_NOISE_RES = [
    re.compile(r"^\d+ of \d+$"),
    re.compile(r"^Document ID \d+: \w+$"),
    re.compile(r"^- ?AUTOSAR CONFIDENTIAL- ?$"),
    re.compile(r"^R\d+(?:\.\d+)+ Rev(?:ision)? \d+$"),
]
_INLINE_RUN_RE = re.compile(r"R\d+(?:\.\d+)+ Rev(?:ision)? \d+\b")
_SENT_RE = re.compile(r"(?<=[.!?;:])\s+|\s*[•¾●]\s*")
SEC_FUZZY = 85
MAX_SENTENCES = 12
MAX_ROWS = 20


class SectionChange(BaseModel):
    change: str                      # added | removed | modified
    section_a: str | None = None
    section_b: str | None = None
    title: str = ""
    title_a: str | None = None
    title_b: str | None = None
    pages_a: list[int | None] = Field(default_factory=list)   # [start, end]
    pages_b: list[int | None] = Field(default_factory=list)
    words_added: int = 0
    words_removed: int = 0
    summary: str = ""
    sentences: list[dict[str, str]] = Field(default_factory=list)   # [{op, old, new}], capped
    rows_added: list[str] = Field(default_factory=list)
    rows_removed: list[str] = Field(default_factory=list)
    renumbered: bool = False
    match_score: float = 100.0
    _gone: Counter = PrivateAttr(default_factory=Counter)
    _new: Counter = PrivateAttr(default_factory=Counter)


class SectionDiff(BaseModel):
    added: list[SectionChange] = Field(default_factory=list)
    removed: list[SectionChange] = Field(default_factory=list)
    modified: list[SectionChange] = Field(default_factory=list)
    front_matter: list[SectionChange] = Field(default_factory=list)   # front matter + change-history tables
    summary: dict[str, int] = Field(default_factory=dict)


Diff.model_rebuild()


def _clean_lines(text: str) -> str:
    lines = [ln for ln in (text or "").split("\n") if not any(r.match(ln.strip()) for r in SEC_NOISE_RES)]
    t = "\n".join(lines).replace("­", "")
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)          # hyphenation at line breaks
    return _INLINE_RUN_RE.sub(" ", t)


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text)
    return [s.strip() for s in _SENT_RE.split(flat) if s and _tokens(s)]


def _title_key(t: str) -> str:
    return " ".join(_tokens(re.sub(r"(?<=[A-Za-z])\d{1,2}$", "", (t or "").strip())))


def _same_content(wa: list[str], wb: list[str]) -> bool:
    """True when two token lists differ only by order or word splitting (same multiset of characters)."""
    return wa == wb or Counter("".join(wa)) == Counter("".join(wb))


def _cells(r: Any) -> str:
    cells = list(r.values()) if isinstance(r, dict) else list(r)
    return " | ".join(re.sub(r"\s+", " ", str(c or "")).strip() for c in cells).strip(" |")


def _row_texts(sec: dict, history: bool = False) -> list[str]:
    out = [_cells(r) for t in sec.get("tables") or [] if (t.get("kind") == "change_history") == history
           for r in t.get("rows") or []]
    return [x for x in out if x]


def _align(a: list[dict], b: list[dict]) -> tuple[list[tuple[dict, dict, float]], list[dict], list[dict]]:
    pairs: list[tuple[dict, dict, float]] = []
    ua, ub = list(a), list(b)
    for score, pred in ((100.0, lambda x, y: x["section_id"] == y["section_id"] and _title_key(x["title"]) == _title_key(y["title"])),
                        (99.0, lambda x, y: bool(_title_key(x["title"])) and _title_key(x["title"]) == _title_key(y["title"]))):
        for x in list(ua):
            for y in list(ub):
                if pred(x, y):
                    pairs.append((x, y, score))
                    ua.remove(x)
                    ub.remove(y)
                    break
    cand = sorted(((fuzz.ratio(_title_key(x["title"]), _title_key(y["title"])), i, j)
                   for i, x in enumerate(ua) for j, y in enumerate(ub)), reverse=True)
    ui, uj = set(), set()
    for s, i, j in cand:
        if i in ui or j in uj or not (s >= SEC_FUZZY or (ua[i]["section_id"] == ub[j]["section_id"] and s >= 60)):
            continue
        ui.add(i)
        uj.add(j)
        pairs.append((ua[i], ub[j], float(s)))
    return pairs, [x for i, x in enumerate(ua) if i not in ui], [y for j, y in enumerate(ub) if j not in uj]


def _pages(s: dict) -> list[int | None]:
    return [s.get("page_start"), s.get("page_end")]


def _cap(s: str, n: int = 400) -> str:
    return s if len(s) <= n else s[:n] + "..."


def _row_delta(ra: list[str], rb: list[str]) -> tuple[list[str], list[str]]:
    na, nb = {_norm_text(r) for r in ra}, {_norm_text(r) for r in rb}
    return [r for r in rb if _norm_text(r) not in na], [r for r in ra if _norm_text(r) not in nb]


def _compare_sections(x: dict, y: dict, score: float) -> SectionChange | None:
    ta, tb = _clean_lines(x.get("text", "")), _clean_lines(y.get("text", ""))
    wa, wb = _tokens(ta), _tokens(tb)
    rows_add, rows_rem = _row_delta(_row_texts(x), _row_texts(y))
    text_same = _same_content(wa, wb)
    if text_same and not rows_add and not rows_rem:
        return None
    add = rem = 0
    sents: list[dict[str, str]] = []
    if not text_same:
        ca, cb = Counter(wa), Counter(wb)       # order-agnostic: figure labels are extracted in unstable order
        add, rem = sum((cb - ca).values()), sum((ca - cb).values())
        sa, sb = _sentences(ta), _sentences(tb)
        ka, kb = [" ".join(_tokens(s)) for s in sa], [" ".join(_tokens(s)) for s in sb]
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes():
            if op != "equal" and not _same_content(_tokens(" ".join(sa[i1:i2])), _tokens(" ".join(sb[j1:j2]))):
                sents.append({"op": op, "old": _cap(" ".join(sa[i1:i2])), "new": _cap(" ".join(sb[j1:j2]))})
        if add == rem == 0 or not sents:   # only reorder/splitting noise survived
            text_same = True
            if not rows_add and not rows_rem:
                return None
    parts = ([f"+{add} / -{rem} words"] if add or rem else []) + \
            ([f"table rows +{len(rows_add)} / -{len(rows_rem)}"] if rows_add or rows_rem else [])
    ch = SectionChange(change="modified", section_a=x["section_id"], section_b=y["section_id"],
                         title=y["title"], title_a=x["title"], title_b=y["title"], pages_a=_pages(x),
                         pages_b=_pages(y), words_added=add, words_removed=rem, summary="; ".join(parts),
                         sentences=sents[:MAX_SENTENCES], rows_added=rows_add[:MAX_ROWS],
                         rows_removed=rows_rem[:MAX_ROWS],
                         renumbered=x["section_id"] != y["section_id"], match_score=score)
    if not text_same:
        ch._gone, ch._new = ca - cb, cb - ca
    return ch


def _drop_moved(mods: list[SectionChange]) -> list[SectionChange]:
    """Drop text-only changes where words vanish from one section and appear in another (figure text that
    straddles a page/section boundary is extracted into a different section between releases)."""
    dead: set[int] = set()
    for i, a in enumerate(mods):
        for j, b in enumerate(mods):
            if i == j or i in dead or j in dead or a.rows_added or a.rows_removed or b.rows_added or b.rows_removed:
                continue
            if a._gone and not a._new and b._new and not b._gone and                     _same_content(list(a._gone.elements()), list(b._new.elements())):
                dead |= {i, j}
    return [m for k, m in enumerate(mods) if k not in dead]


def _sec_key(c: SectionChange) -> list[int]:
    return [int(p) if p.isdigit() else 0 for p in (c.section_b or c.section_a or "0").split(".")]


def section_diff(doc_a: dict, doc_b: dict) -> SectionDiff:
    """Text/section diff of two processed document JSONs (A -> B); see the design note above."""
    out = SectionDiff()
    secs_a, secs_b = doc_a.get("sections", []), doc_b.get("sections", [])
    for x, y in zip([s for s in secs_a if s["section_id"] == "0"], [s for s in secs_b if s["section_id"] == "0"]):
        ch = _compare_sections(x, y, 100.0)
        if ch:
            ch.title = "Front matter"
            out.front_matter.append(ch)
    radd, rrem = _row_delta([r for s in secs_a for r in _row_texts(s, True)],
                            [r for s in secs_b for r in _row_texts(s, True)])
    if radd or rrem:
        out.front_matter.append(SectionChange(change="modified", title="Change history",
                                              summary=f"revision rows +{len(radd)} / -{len(rrem)}",
                                              rows_added=radd[:MAX_ROWS], rows_removed=rrem[:MAX_ROWS]))
    pairs, left_a, left_b = _align([s for s in secs_a if s["section_id"] != "0"],
                                   [s for s in secs_b if s["section_id"] != "0"])
    out.modified = _drop_moved([ch for x, y, sc in pairs if (ch := _compare_sections(x, y, sc))])
    for x in left_a:
        n = len(_tokens(_clean_lines(x["text"])))
        out.removed.append(SectionChange(change="removed", section_a=x["section_id"], title=x["title"],
                                         title_a=x["title"], pages_a=_pages(x), words_removed=n, summary=f"-{n} words"))
    for y in left_b:
        n = len(_tokens(_clean_lines(y["text"])))
        out.added.append(SectionChange(change="added", section_b=y["section_id"], title=y["title"],
                                       title_b=y["title"], pages_b=_pages(y), words_added=n, summary=f"+{n} words"))
    for lst in (out.added, out.removed, out.modified):
        lst.sort(key=_sec_key)
    out.summary = {"added": len(out.added), "removed": len(out.removed), "modified": len(out.modified),
                   "front_matter": len(out.front_matter)}
    return out


def _sections_for(project_id: str, doc_a: str, doc_b: str, processed_dir=None) -> SectionDiff | None:
    from app.ingest import load_processed
    try:
        return section_diff(load_processed(project_id, doc_a, processed_dir or settings.processed_dir),
                            load_processed(project_id, doc_b, processed_dir or settings.processed_dir))
    except (OSError, ValueError, KeyError):
        return None
