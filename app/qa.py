"""Grounded question answering over one project's documents (local Ollama LLM).

Pipeline: membership check (viewer+) -> retrieve (``retrieve.retrieve``: dense + BM25 fused, release policy,
inventory evidence, section expansion) -> refuse if the best dense score of the retrieved text chunks is below
``settings.similarity_threshold`` and no inventory component named in the question matched -> labelled prompt [C1]..[Cn] -> LLM generation -> post-processing
(``postprocess``: split into sentences, drop invented labels, drop every sentence that has no valid label, keep
"Not stated in the provided documents" notes) -> if nothing valid remains, return the refusal -> confidence ->
stored in ``answers`` + audit log.

Embedder fallback: when the hashing embedder is in use (not bge) ``ask`` refuses with Low confidence and the
limitation "Embedder fallback active ..." (unless ALLOW_HASH_QA=1, used by the unit tests).

Confidence rule (``compute_confidence``, purely rule-based, never asked of the LLM):
  * Low    : refused, evidence-only (no LLM), generation fallback, no valid [Cn] citation, or fewer than half of the
             generated sentences survived citation filtering (share of sentences cited < 0.5).
  * High   : mode "generated", top retrieval score >= HIGH_SCORE (settings.high_score) AND >= 2 distinct valid
             cited chunks AND share of sentences cited >= 0.8.
  * Medium : everything else that is generated with at least one valid cited chunk.
  With an evidence-support check (``evidence_support``, used by ``ask``): a cited sentence is supported when its
  numbers and identifiers occur in one of its cited chunks and >= 60% of its content words do (for an inventory
  chunk the numbers and identifiers suffice). High additionally needs every claim supported AND (>= 2 distinct chunks that
  support a claim, or inventory backing); fewer than half of the claims supported -> Low. Change-history answers
  are High. Inventory list answers (``inventory_confidence``): Medium when any listed row has Medium/Low extraction
  confidence or a row rests on one non-table source only (e.g. a diagram); High only when every row is table-derived
  with High confidence or is confirmed by a second source (a retrieved text chunk naming it, or a second extraction
  method).
"""
from __future__ import annotations

import inspect
import json
import os
import re
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, Field

from app import controls, llm, retrieve
from app.config import settings

HIGH_SCORE = settings.high_score
REFUSAL_TEXT = "I cannot answer this from the project documents."
NOT_STATED = "Not stated in the provided documents"
MIN_CITED_SHARE, HIGH_CITED_SHARE = 0.5, 0.8
IDENTIFIER_MARGIN = 0.05  # set on the dev set (eval/dev_results.md), never on the gold set
EVIDENCE_ONLY_TEXT = "LLM not configured — showing top retrieved evidence"
SYSTEM_PROMPT = (
    "You answer questions about technical architecture documents using ONLY the evidence chunks provided, which "
    "are labelled [C1], [C2], ... Rules: 1) Write 1-4 short sentences. 2) End EVERY sentence, including the "
    "first, with the chunk label(s) it comes from, written exactly like [C1] or [C1, C3]; never put component "
    f"names in brackets. 3) If the evidence does not cover the question or part of it, write exactly: {NOT_STATED}. "
    "4) No outside knowledge. 5) A chunk starting with 'EXTRACTED INVENTORY' lists ports/signals found in the "
    "document's diagrams and tables: when it lists items, state them; never say a component has no ports when "
    "such a chunk lists some. Example: The FooManager controls the lamp [C2]. It reads the switch state [C1, C3].")

SCHEMA = """
CREATE TABLE IF NOT EXISTS answers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, user TEXT, question TEXT, answer TEXT,
  confidence TEXT, mode TEXT, citations_json TEXT, limitation TEXT, created_at TEXT,
  review_status TEXT NOT NULL DEFAULT 'unreviewed');
"""


class Citation(BaseModel):
    label: str
    doc_id: str | None = None
    version: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    section_id: str | None = None
    section_title: str | None = None
    score: float = 0.0
    ocr: bool = False
    ocr_confidence: float | None = None
    note: str = ""


class Answer(BaseModel):
    id: int | None = None
    project_id: str = ""
    question: str = ""
    answer: str
    confidence: Literal["High", "Medium", "Low"]
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    limitation: str = ""
    mode: Literal["generated", "evidence_only", "refused"]
    review_status: str = "unreviewed"
    model: str = ""  # local model that wrote some of the answer text ("" when no model wrote any of it)
    refusal_reason: str = ""  # refused: "off_topic", "not_found" (on topic, not stated) or "not_extracted"
    answer_type: Literal["text", "inventory_list"] = "text"
    built_from: Literal["llm", "inventory", "inventory+llm", "change_history", "section_lookup", ""] = ""
    inventory: dict[str, Any] | None = None  # inventory_list: {"blocks": [...], "description": "... [Cn]."}
    confidence_reason: str = ""  # one line for the confidence tooltip ("" = derived by the client)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def ocr_note(m: dict) -> str:
    """Limitation note for a chunk read from a scanned page ('' otherwise)."""
    if m.get("inventory"):
        return "from extracted inventory"
    if not m.get("ocr"):
        return ""
    c = m.get("ocr_confidence")
    return f"from OCR, confidence {c:.0f}" if isinstance(c, (int, float)) and c >= 0 else "from OCR"


def _ocr_limitation(limitation: str, cites: list) -> str:
    notes = sorted({f"{c.label} {c.note}" for c in cites if c.note and c.ocr})
    if not notes:
        return limitation
    return (limitation + " OCR text may contain recognition errors (" + "; ".join(notes) + ").").strip()


def _citation(label: str, hit: dict) -> Citation:
    m = hit["metadata"]
    return Citation(label=label, doc_id=m.get("doc_id"), version=m.get("version"), page_start=m.get("page_start"),
                    page_end=m.get("page_end"), section_id=m.get("section_id"),
                    section_title=m.get("section_title"), score=round(hit["score"], 4),
                    ocr=bool(m.get("ocr")), ocr_confidence=(m.get("ocr_confidence") if m.get("ocr") else None),
                    note=ocr_note(m))


def label_map(hits: list[dict]) -> dict[str, dict]:
    """{"C1": hit, ...} in rank order."""
    return {f"C{i}": h for i, h in enumerate(hits, 1)}


def build_prompt(question: str, hits: list[dict]) -> tuple[str, str]:
    """Return (prompt, system). Chunks are labelled [C1]..[Cn] with their source location."""
    parts = []
    for label, h in label_map(hits).items():
        m = h["metadata"]
        parts.append(f"[{label}] ({m.get('doc_id')} v{m.get('version')}, p.{m.get('page_start')}, "
                     f"section {m.get('section_id')} {m.get('section_title')})\n{h['text']}")
    prompt = "EVIDENCE:\n" + "\n\n".join(parts) + f"\n\nQUESTION: {question}\n\nANSWER (every sentence ends with a label like [C1]):"
    return prompt, SYSTEM_PROMPT


_LABEL = re.compile(r"\[\s*(C\d+(?:\s*,\s*C\d+)*)\s*\]")


def parse_citations(answer_text: str, lmap: dict[str, Any]) -> list[str]:
    """Labels cited in the text, in first-use order, keeping only labels present in lmap (invented ones dropped)."""
    seen: list[str] = []
    for m in _LABEL.finditer(answer_text):
        for lab in re.split(r"\s*,\s*", m.group(1)):
            if lab in lmap and lab not in seen:
                seen.append(lab)
    return seen


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[])|\n+")


def split_sentences(text: str) -> list[str]:
    """Sentence split; a label placed after the full stop ('. [C1]') is moved back into its sentence."""
    text = re.sub(r"\]\s*\[(?=\s*C\d)", ", ", text)  # [C1][C2] -> [C1, C2]
    text = re.sub(r"([.!?])\s*(\[\s*C\d+(?:\s*,\s*C\d+)*\s*\])", r" \2\1", text)
    return [x.strip() for x in _SENT_SPLIT.split(text) if x and x.strip()]


def _keep_valid(group: str, lmap: dict) -> str:
    ok = [x for x in re.split(r"\s*,\s*", group) if x in lmap]
    return f"[{', '.join(ok)}]" if ok else ""


def postprocess(text: str, lmap: dict[str, Any]) -> tuple[str, list[str], float]:
    """Keep only sentences with at least one valid label. Returns (clean_text, labels_in_order, cited_share).

    Invented labels are removed from kept sentences. "Not stated ..." sentences are kept (they are statements of
    absence, not claims) but do not count as cited. cited_share = kept cited / (cited + dropped) sentences.
    """
    kept: list[str] = []
    labels: list[str] = []
    dropped = 0
    for sent in split_sentences(text):
        valid = parse_citations(sent, lmap)
        clean = _LABEL.sub(lambda m: _keep_valid(m.group(1), lmap), sent)
        clean = re.sub(r"\s+([.!?,])", r"\1", clean).strip()
        if valid:
            kept.append(clean)
            labels += [x for x in valid if x not in labels]
        elif NOT_STATED.lower() in sent.lower():
            kept.append(clean)
        elif re.sub(r"[\W_]+", "", sent):
            dropped += 1
    n_cited = sum(1 for k in kept if parse_citations(k, lmap))
    share = n_cited / (n_cited + dropped) if (n_cited + dropped) else 0.0
    return " ".join(kept), labels, share


SUPPORT_OVERLAP = 0.6
_NUM = re.compile(r"(?<![A-Za-z\d])\d+(?:[.,]\d+)?")


def _relations(text: str, inventory: bool = False) -> set[tuple]:
    """Conservative named subject/action/object checks; not a general entailment model."""
    ident = r"[A-Z][A-Za-z0-9_]*"
    pattern = (rf"\b({ident})\s+(?:(does\s+not|never)\s+)?(provides?|requires?|sends?|receives?)\s+"
               rf"(?:the\s+)?(?:(?:port|signal|interface)\s+)?({ident})\b"
               rf"(?:\s+(to|from)\s+(?:the\s+)?(?:component\s+)?({ident}))?")
    found = set()
    for m in re.finditer(pattern, text):
        subject, neg, verb, obj, prep, peer = m.groups()
        action = next(x for x in ("provide", "require", "send", "receive") if verb.startswith(x))
        found.add((subject.lower(), action, obj.lower(), bool(neg)))
        if peer and action == "send" and prep == "to":
            found.add((peer.lower(), "receive", obj.lower(), bool(neg)))
        if peer and action == "receive" and prep == "from":
            found.add((peer.lower(), "send", obj.lower(), bool(neg)))
    for m in re.finditer(rf"\b(provided|required)\s+port\s+({ident})\s+(?:of|on)\s+({ident})", text):
        direction, port, owner = m.groups()
        found.add((owner.lower(), "provide" if direction == "provided" else "require", port.lower(), False))
    if inventory:
        owner = re.search(r"for component ([A-Z][A-Za-z0-9_]*)", text)
        for match in re.finditer(r"\bport ([A-Z][A-Za-z0-9_]*)(?: of ([A-Z][A-Za-z0-9_]*))? \(direction (provided|required)", text):
            port, row_owner, direction = match.groups()
            component = row_owner or (owner.group(1) if owner else "")
            if component:
                found.add((component.lower(), "provide" if direction == "provided" else "require", port.lower(), False))
        for m in re.finditer(rf"\bsignal ({ident}) \(sender ({ident}), receivers ([^\n]+?), page ", text):
            signal, sender, receivers = m.groups()
            if sender != "unknown":
                found.add((sender.lower(), "send", signal.lower(), False))
            for receiver in receivers.split(", "):
                if re.fullmatch(ident, receiver) and receiver != "unknown":
                    found.add((receiver.lower(), "receive", signal.lower(), False))
    return found


def sentence_supported(sentence: str, chunk: str, inventory: bool = False) -> bool:
    """Numbers and identifiers of the sentence occur in the chunk, and >= SUPPORT_OVERLAP of its content words do.
    An inventory chunk (a list of rows) supports a sentence whose numbers and identifiers it contains."""
    s = _LABEL.sub("", sentence)
    low = chunk.lower()
    if any(n not in chunk for n in _NUM.findall(s)):
        return False
    if any(not retrieve.contains_identifier(chunk, i) for i in retrieve.identifiers_in(s)):
        return False
    if not _relations(s).issubset(_relations(chunk, inventory)):
        return False
    if inventory:
        return True
    words = set(retrieve.tokenize(s))
    have = set(retrieve.tokenize(chunk))
    return not words or len(words & have) / len(words) >= SUPPORT_OVERLAP


def evidence_support(text: str, lmap: dict[str, dict]) -> dict:
    """{claims, supported, agreeing, inventory}: claim sentences (not 'Not stated ...'), those supported by one of
    their cited chunks, distinct chunks that support some claim, and whether an inventory chunk supports one."""
    claims = supported = 0
    agree: set[str] = set()
    inv = False
    for sent in split_sentences(text):
        labs = parse_citations(sent, lmap)
        if not labs or NOT_STATED.lower() in sent.lower():
            continue
        claims += 1
        ok = [lab for lab in labs if sentence_supported(sent, lmap[lab]["text"],
                                                        lmap[lab]["metadata"].get("inventory"))]
        if ok:
            supported += 1
            agree.update(ok)
            inv = inv or any(lmap[lab]["metadata"].get("inventory") for lab in ok)
    return {"claims": claims, "supported": supported, "agreeing": len(agree), "inventory": inv}


def supported_only(text: str, lmap: dict[str, dict]) -> tuple[str, list[str], int]:
    kept, dropped = [], 0
    for sent in split_sentences(text):
        labels = parse_citations(sent, lmap)
        if NOT_STATED.lower() in sent.lower():
            kept.append(sent)
            continue
        supported = [lab for lab in labels if sentence_supported(
            sent, lmap[lab]["text"], bool(lmap[lab]["metadata"].get("inventory")))]
        if not supported:
            dropped += 1
            continue
        def repair(match):
            valid = [x for x in re.split(r"\s*,\s*", match.group(1)) if x in supported]
            return f"[{', '.join(valid)}]" if valid else ""
        kept.append(_LABEL.sub(repair, sent))
    clean = " ".join(kept)
    return clean, parse_citations(clean, lmap), dropped


def compute_confidence(mode: str, top_score: float, n_valid: int, cited_share: float = 1.0,
                       support: dict | None = None) -> str:
    """See module docstring for the rule (``support`` from ``evidence_support``; None = citation rule only)."""
    if mode != "generated" or n_valid == 0 or cited_share < MIN_CITED_SHARE:
        return "Low"
    if support is not None and support["claims"] and support["supported"] / support["claims"] < 0.5:
        return "Low"
    high = top_score >= HIGH_SCORE and n_valid >= 2 and cited_share >= HIGH_CITED_SHARE
    if support is not None:
        high = (high and support["supported"] == support["claims"]
                and (support["agreeing"] >= 2 or support["inventory"]))
    return "High" if high else "Medium"


def _store(c: sqlite3.Connection, ans: Answer, user: str) -> None:
    ensure_schema(c)
    cur = c.execute(
        "INSERT INTO answers(project_id,user,question,answer,confidence,mode,citations_json,limitation,created_at,"
        "review_status) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ans.project_id, user, ans.question, ans.answer, ans.confidence, ans.mode,
         json.dumps([x.model_dump() for x in ans.citations]), ans.limitation, controls._now(), ans.review_status))
    c.commit()
    ans.id = cur.lastrowid
    controls.log_action(user, "ask", ans.project_id, {"answer_id": ans.id, "mode": ans.mode,
                                                      "confidence": ans.confidence, "question": ans.question[:200]}, c)


def _generate(provider, prompt: str, system: str, model: str | None, chunk_ids: list):
    """Call provider.generate with model/purpose/chunk_ids when it supports them (simple fakes may not)."""
    params = inspect.signature(provider.generate).parameters
    kw = {k: v for k, v in (("model", model), ("purpose", "qa"), ("chunk_ids", chunk_ids)) if k in params}
    return provider.generate(prompt, system, **kw)


def _inventory_summary(h: dict) -> dict:
    """Short description of an inventory evidence chunk for the Sources list (its raw text is not shown)."""
    rows = h.get("rows") or {}
    return {"inventory_subject": rows.get("component") or rows.get("identifier")
            or h["metadata"].get("extraction_gap") or "",
            "inventory_counts": {t: len(rows.get(t) or []) for t in ("ports", "signals")}}


def inventory_hits(project_id: str, question: str, c: sqlite3.Connection, max_docs: int = 2) -> list[dict]:
    """Inventory evidence chunks for a question (see ``retrieve.inventory_evidence``): ports/signals of a component
    named in a ports/interfaces/signals/connection question, or an explicit "possible extraction gap" chunk."""
    from app import extract
    inv = extract.get_inventory(project_id, conn=c)
    docs = retrieve.project_docs(project_id, c)
    info = retrieve.analyze_query(question, docs, retrieve.inventory_names(inv))
    return retrieve.inventory_evidence(project_id, info, c, docs, max_docs=max_docs, inv=inv)


# ---------------------------------------------------------------- structured (inventory) answers
# List/connection questions ("which ports/interfaces/signals ...", "who sends X", "through which port ... between A
# and B") are answered deterministically from the inventory rows of the evidence; the LLM only adds one description
# sentence from the text chunks. Questions about attributes the inventory does not hold fall back to the LLM.
_STRUCT_Q = re.compile(r"\b(which|what|list|name)\b[^?]*\b(ports?|interfaces?|signals?|data elements?)\b"
                       r"|\b(who|which (?:component|swc|module|software component)s?)\b[^?]*\b(provid\w*|send\w*|"
                       r"receiv\w*|requir\w*|consum\w*|produc\w*)\b|\bthrough which port\b", re.I)
_ATTR_Q = re.compile(r"\b(data ?type|type of|units?|range|resolution|initial value|timing|period|cycle|mean\w*|"
                     r"purpose|why|how)\b", re.I)
_OUT_W = re.compile(r"\b(provid\w*|send\w*|sent|output\w*|report\w*|publish\w*|offer\w*|produc\w*|transmit\w*)\b", re.I)
_IN_W = re.compile(r"\b(requir\w*|receiv\w*|consum\w*|input\w*|reads?|uses?|subscrib\w*)\b", re.I)
_NEGATION = re.compile(r"\b(no|not|none|never|without)\b[^.]*\b(ports?|interfaces?|signals?|data elements?)\b"
                       r"|\b(ports?|interfaces?|signals?)\b[^.]*\b(not (?:explicitly )?(?:listed|stated|shown|defined))\b",
                       re.I)
DESC_SYSTEM = (
    "You describe architecture elements using ONLY the evidence chunks provided, labelled [C1], [C2], ... Write ONE "
    "short sentence saying what the named element does or is responsible for, ending with the label(s) it comes from, "
    "e.g. [C2]. Do not list ports, interfaces or signals. If the evidence does not describe it, write exactly: "
    f"{NOT_STATED}.")


def _direction_filter(question: str) -> str | None:
    out, inn = bool(_OUT_W.search(question)), bool(_IN_W.search(question))
    return None if out == inn else ("provided" if out else "required")


def _ver(v) -> str:
    return "" if not v else (f"v{v}" if str(v)[:1].isdigit() else str(v))


def _main_page(rows: list[dict]) -> int | None:
    """The page most rows are on (lowest on a tie)."""
    pages = [r.get("page") for r in rows if isinstance(r.get("page"), int)]
    return min(set(pages), key=lambda p: (-pages.count(p), p)) if pages else None


def _where(doc_id, version, page, section_id) -> str:
    """'bodycomfort_r3.2.3, R3.2.3, p.14 §4.3'"""
    loc = " ".join(x for x in (f"p.{page}" if page else "", f"§{section_id}" if section_id else "") if x)
    return ", ".join(x for x in (doc_id, _ver(version), loc) if x)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun if n != 1 else noun[:-1]}"


_PORT_COLS = [["name", "Port"], ["direction", "Direction"], ["interface", "Interface"], ["page", "Page"]]
_IF_COLS = [["interface", "Interface"], ["name", "Port"], ["direction", "Direction"], ["page", "Page"]]
_SIG_COLS = [["name", "Signal"], ["sender", "Sender"], ["receivers", "Receivers"], ["page", "Page"]]
_OWNER_COLS = [["name", "Port"], ["component", "Component"], ["direction", "Direction"], ["interface", "Interface"],
               ["page", "Page"]]
_DIRECTIONS = (("required", "Required"), ("provided", "Provided"), ("unknown", "Unknown"))


def _row(r: dict, cols: list[list[str]], group_page) -> dict:
    """API row: the table columns plus method/confidence; page only when it differs from the group's page."""
    out: dict[str, Any] = {}
    for k, _ in cols:
        if k == "receivers":
            out[k] = list(r.get(k) or [])
        elif k != "page":
            out[k] = r.get(k) or ("unknown" if k == "direction" else "")
    out["page"] = r.get("page") if r.get("page") != group_page else None
    out.update(method=r.get("extraction_method") or "", confidence=r.get("confidence") or "")
    return out


def _group(title: str, rows: list[dict], cols: list[list[str]], noun: str) -> dict:
    page = _main_page(rows)
    return {"title": title, "noun": noun, "count": len(rows), "page": page, "columns": cols,
            "rows": [_row(r, cols, page) for r in rows]}


def _item(r: dict, kind: str, page) -> str:
    """One list item of the plain-text answer; its page only when it differs from the answer's page."""
    at = f" p.{r['page']}" if isinstance(r.get("page"), int) and r.get("page") != page else ""
    if kind == "interfaces":
        return (f"{r['interface']} (port {r['name']}){at}" if r.get("interface")
                else f"port {r['name']} (no interface extracted){at}")
    if kind == "sent":
        return f"{r['name']} (to {', '.join(r.get('receivers') or []) or 'unknown receivers'}){at}"
    if kind == "received":
        return f"{r['name']} (from {r.get('sender') or 'unknown sender'}){at}"
    return f"{r['name']}" + (f" ({r['interface']})" if r.get("interface") else "") + at


def _rows_for_component(rows: dict, question: str) -> tuple[str, list[dict], list[dict]] | None:
    """(noun, ports, signals) of one component, filtered by the direction and kind the question asks for."""
    norm = rows.get("norm", "")
    ports, sigs = rows.get("ports") or [], rows.get("signals") or []
    direction = _direction_filter(question)
    want_if = bool(re.search(r"\binterfaces?\b", question, re.I))
    want_sig = bool(re.search(r"\b(signals?|data elements?)\b", question, re.I))
    want_port = bool(re.search(r"\bports?\b", question, re.I))
    if direction:
        known = [p for p in ports if p.get("direction") == direction]
        ports = known or [p for p in ports if (p.get("direction") or "unknown") == "unknown"]
        sent = [s for s in sigs if retrieve._norm(s.get("sender") or "") == norm]
        recv = [s for s in sigs if norm in [retrieve._norm(r) for r in s.get("receivers") or []]]
        sigs = sent if direction == "provided" else recv
    if want_if and ports:
        return "interfaces", ports, []
    if want_sig and sigs:
        return "signals", [], sigs
    if (want_port or want_if or want_sig) and ports:
        return "ports", ports, []
    if ports or sigs:
        return "ports and signals", ports, sigs
    return None


def _component_block(lab: str, h: dict, question: str) -> tuple[str, dict, list[dict]] | None:
    """(sentence, block, listed rows) for the ports/interfaces/signals of one component in one document."""
    rows, m = h["rows"], h["metadata"]
    got = _rows_for_component(rows, question)
    if not got:
        return None
    noun, ports, sigs = got
    listed = ports + sigs
    page = _main_page(listed)
    groups, phrases = [], []
    port_noun = "interfaces" if noun == "interfaces" else "ports"
    for key, title in _DIRECTIONS:
        sel = [p for p in ports if (p.get("direction") or "unknown") == key]
        if not sel:
            continue
        groups.append(_group(title, sel, _IF_COLS if noun == "interfaces" else _PORT_COLS, port_noun))
        adj = "" if key == "unknown" else f"{key} "
        tail = " with no direction extracted" if key == "unknown" else ""
        if noun == "interfaces":  # interfaces found, then the ports that have none (said once, not per port)
            have = [p for p in sel if p.get("interface")]
            none = [p for p in sel if not p.get("interface")]
            if have:
                phrases.append(f"{len(have)} {adj}interface{'s' if len(have) != 1 else ''}{tail}: "
                               + ", ".join(_item(p, "interfaces", page) for p in have))
            if none:
                phrases.append(f"{len(none)} {adj}port{'s' if len(none) != 1 else ''}{tail} with no interface "
                               "extracted: " + ", ".join(_item(p, "ports", page) for p in none))
        else:
            phrases.append(f"{len(sel)} {adj}port{'s' if len(sel) != 1 else ''}{tail}: "
                           + ", ".join(_item(p, "ports", page) for p in sel))
    norm = rows.get("norm", "")
    sent = [s for s in sigs if retrieve._norm(s.get("sender") or "") == norm]
    recv = [s for s in sigs if s not in sent]
    sig_phrases = []
    for kind, title, verb, sel in (("sent", "Sent", "sends", sent), ("received", "Received", "receives", recv)):
        if sel:
            groups.append(_group(title, sel, _SIG_COLS, "signals"))
            sig_phrases.append(f"{verb} {_count(len(sel), 'signals')}: " + ", ".join(_item(s, kind, page) for s in sel))
    body = ("has " + "; ".join(phrases)) if phrases else ""
    if sig_phrases:
        body = (body + "; and " if body else "") + "; ".join(sig_phrases)
    comp = rows.get("component", "")
    text = f"{comp} ({_where(m.get('doc_id'), m.get('version'), page, m.get('section_id'))}) {body} [{lab}]."
    block = {"label": lab, "kind": "component", "subject": comp, "noun": noun, "doc_id": m.get("doc_id"),
             "version": m.get("version"), "page": page, "section_id": m.get("section_id"), "groups": groups}
    return text, block, listed


def _identifier_block(lab: str, h: dict) -> tuple[str, dict, list[dict]] | None:
    """(sentence, block, rows) for the port/signal rows of an identifier named in the question."""
    rows, m = h["rows"], h["metadata"]
    sigs, ports = rows.get("signals") or [], rows.get("ports") or []
    listed = sigs + ports
    if not listed:
        return None
    page = _main_page(listed)
    at = lambda r: f" (p.{r['page']})" if r.get("page") and r.get("page") != page else ""  # noqa: E731
    parts = [f"signal {s['name']} is sent by {s.get('sender') or 'an unknown sender'} and received by "
             f"{', '.join(s.get('receivers') or []) or 'unknown receivers'}{at(s)}" for s in sigs]
    parts += [f"port {p['name']} belongs to {p.get('component') or 'an unknown component'} "
              f"({p.get('direction') or 'direction not extracted'}"
              + (f", interface {p['interface']}" if p.get("interface") else "") + f"){at(p)}" for p in ports]
    text = f"In {_where(m.get('doc_id'), m.get('version'), page, m.get('section_id'))}, {'; '.join(parts)} [{lab}]."
    groups = ([_group("Signal", sigs, _SIG_COLS, "signals")] if sigs else []) + \
             ([_group("Port", ports, _OWNER_COLS, "ports")] if ports else [])
    block = {"label": lab, "kind": "identifier", "subject": rows.get("identifier", ""),
             "noun": "signals" if not ports else "ports" if not sigs else "rows",
             "doc_id": m.get("doc_id"), "version": m.get("version"), "page": page,
             "section_id": m.get("section_id"), "groups": groups}
    return text, block, listed


def structured_blocks(question: str, info: "retrieve.QueryInfo",
                      lmap: dict[str, dict]) -> tuple[str, list[str], list[dict], list[dict]] | None:
    """(plain text, labels, blocks for the table view, listed inventory rows) built only from inventory rows, or None
    when the question is not a list/connection question, an identifier it names is not in the rows, or it asks for
    an attribute the inventory does not hold (data type, meaning, timing ...). The text cites each inventory chunk
    once, after its whole list, never after each item."""
    if not _STRUCT_Q.search(question) or _ATTR_Q.search(question):
        return None
    inv = [(lab, h) for lab, h in lmap.items() if h.get("rows") and not h["metadata"].get("extraction_gap")]
    if not inv:
        return None
    names = {retrieve._norm(x) for _, h in inv for t in ("ports", "signals") for r in h["rows"].get(t) or []
             for x in [r.get("name") or "", r.get("interface") or ""]}
    comps = {h["rows"].get("norm") for _, h in inv}
    for ident in info.identifiers:  # a port/signal/interface named in the question must be in the rows
        n = retrieve._norm(ident)
        if n not in names and n not in comps and not any(n == c or n in c for c in info.components):
            return None
    comp_hits = [(lab, h) for lab, h in inv if h["rows"].get("kind") == "component"]
    if len(comp_hits) >= 2 and re.search(r"\b(between|exchanged|connect\w*|shared?)\b", question, re.I):
        (la, a), (lb, b) = comp_hits[:2]
        na = {retrieve._norm(r["name"]): r for t in ("ports", "signals") for r in a["rows"].get(t) or []}
        nb = {retrieve._norm(r["name"]) for t in ("ports", "signals") for r in b["rows"].get(t) or []}
        shared = [na[n] for n in na if n in nb]
        if shared:
            ca, cb, m = a["rows"]["component"], b["rows"]["component"], a["metadata"]
            page = _main_page(shared)
            text = (f"{ca} and {cb} ({_where(m.get('doc_id'), m.get('version'), page, m.get('section_id'))}) share "
                    f"{_count(len(shared), 'items')}: {', '.join(_item(r, 'ports', page) for r in shared)} [{la}, {lb}].")
            block = {"label": la, "labels": [la, lb], "kind": "shared", "subject": f"{ca} and {cb}", "noun": "items",
                     "doc_id": m.get("doc_id"), "version": m.get("version"), "page": page,
                     "section_id": m.get("section_id"),
                     "groups": [_group("Shared", shared, [["name", "Name"], ["page", "Page"]], "items")]}
            return text, [la, lb], [block], shared
    sents, labels, blocks, listed = [], [], [], []
    for lab, h in inv:
        got = (_identifier_block(lab, h) if h["rows"].get("kind") == "identifier"
               else _component_block(lab, h, question))
        if not got:
            continue
        sents.append(got[0])
        blocks.append(got[1])
        listed += got[2]
        labels.append(lab)
    return (" ".join(sents), labels, blocks, listed) if sents else None


def structured_answer(question: str, info: "retrieve.QueryInfo", lmap: dict[str, dict]) -> tuple[str, list[str]] | None:
    """(text, labels) of ``structured_blocks``; None when the question is not answered from the inventory."""
    got = structured_blocks(question, info, lmap)
    return (got[0], got[1]) if got else None


_METHOD_TEXT = {"diagram": "read from a diagram", "llm": "read by the model", "heuristic": "found from headings",
                "convention": "guessed from names", "table": "read from a table"}


def inventory_confidence(rows: list[dict], lmap: dict[str, dict], noun: str = "rows") -> tuple[str, str]:
    """(confidence, reason) of an answer listed from inventory rows.

    Medium when any row has Medium/Low extraction confidence, or when a row rests on one non-table source only
    (e.g. read from a diagram and named nowhere else). High only when every row is table-derived with High
    confidence, or is confirmed by a second, independent source: a second extraction method among its sources
    ("convention" does not count), or a retrieved chunk that names it and was not the row's own source, i.e. a
    table chunk, or a text chunk that is not diagram label text and does not cover the row's page."""
    n = len(rows)
    if not n:
        return "Low", "no inventory rows listed"
    weak = [r for r in rows if (r.get("confidence") or "High") != "High"]
    if weak:
        levels = "/".join(x for x in ("Medium", "Low") if any(r.get("confidence") == x for r in weak)) or "lower"
        return "Medium", f"{len(weak)} of {n} {noun} have {levels} extraction confidence"
    if all(r.get("extraction_method") == "table" for r in rows):
        return "High", f"all {noun} read from a table with High confidence"
    chunks = [h for h in lmap.values() if not h["metadata"].get("inventory")
              and h["metadata"].get("chunk_type") != "figure_text"]  # diagram labels = the diagram itself

    def independent(h: dict, r: dict) -> bool:
        m = h["metadata"]
        if m.get("chunk_type") == "table":
            return r.get("extraction_method") != "table"
        lo, hi = m.get("page_start"), m.get("page_end") or m.get("page_start")
        page = r.get("page")
        return not (isinstance(page, int) and isinstance(lo, int) and lo <= page <= hi)

    def second_source(r: dict) -> bool:
        methods = {r.get("extraction_method")} | {s.get("method") for s in r.get("sources") or []}
        if len(methods - {None, "", "convention"}) >= 2:
            return True
        return any(independent(h, r) and retrieve.contains_identifier(h["text"], x)
                   for h in chunks for x in (r.get("name"), r.get("interface")) if x)

    single = [r for r in rows if r.get("extraction_method") != "table" and not second_source(r)]
    if not single:
        return "High", f"all {noun} confirmed by a table, the document text or a second extraction method"
    methods = {r.get("extraction_method") for r in single}
    how = _METHOD_TEXT.get(next(iter(methods)), "from one source") if len(methods) == 1 else "from one non-table source"
    who = f"all {noun}" if len(single) == n else f"{len(single)} of {n} {noun}"
    return "Medium", f"{who} {how}, with no second source"


_CONFLICT_NOTE = ("The model's text described the ports/interfaces/signals differently from the extracted inventory; "
                  "the inventory was used.")


def _row_names(lmap: dict) -> tuple[dict[str, str], dict[str, str]]:
    """(items, components) of the inventory evidence rows, normalised -> name: items are port, interface and signal
    names; components are owners, senders and receivers."""
    items: dict[str, str] = {}
    comps: dict[str, str] = {}
    for h in lmap.values():
        for t in ("ports", "signals"):
            for r in (h.get("rows") or {}).get(t) or []:
                for x in (r.get("name"), r.get("interface")):
                    if x:
                        items.setdefault(retrieve._norm(x), x)
                for x in [r.get("sender"), r.get("component")] + list(r.get("receivers") or []):
                    for part in (x or "").split("::"):
                        if part.strip():
                            comps.setdefault(retrieve._norm(part), part.strip())
    return items, comps


_SEND_V = re.compile(r"\b(send\w*|sent|provid\w*|publish\w*|transmit\w*|output\w*)\b", re.I)
_RECV_V = re.compile(r"\b(receiv\w*|consum\w*|read\w*|input\w*)\b", re.I)


def _description(provider, question: str, subject: str, lmap: dict, model: str | None,
                 listed: str = "") -> tuple[str, str]:
    """(one cited description sentence from the text chunks or '', limitation note). The sentence is dropped when it
    restates ports/interfaces/signals of the inventory rows (the list is the inventory's job); when it says there
    are none, or names them differently from the listed answer, the note says the inventory was trusted."""
    text_hits = {lab: h for lab, h in lmap.items() if not h["metadata"].get("inventory")}
    if not text_hits or provider is None:
        return "", ""
    parts = [f"[{lab}] ({h['metadata'].get('doc_id')} p.{h['metadata'].get('page_start')})\n{h['text']}"
             for lab, h in text_hits.items()]
    prompt = ("EVIDENCE:\n" + "\n\n".join(parts) + f"\n\nQUESTION: {question}\nELEMENT: {subject}\n\n"
              "ONE SENTENCE (ends with a label like [C2]):")
    try:
        params = inspect.signature(provider.generate).parameters
        kw = {k: v for k, v in (("model", model), ("purpose", "qa_desc"),
                                ("chunk_ids", [h["id"] for h in text_hits.values()])) if k in params}
        raw = provider.generate(prompt, DESC_SYSTEM, **kw)
    except (llm.LLMNotConfigured, NotImplementedError):
        return "", ""
    clean, labels, _ = postprocess(raw if isinstance(raw, str) else json.dumps(raw), text_hits)
    first = next((s for s in split_sentences(clean) if parse_citations(s, text_hits)), "")
    if first and _NEGATION.search(first):
        return "", _CONFLICT_NOTE
    items, comps = _row_names(lmap)
    subj, low = retrieve._norm(subject), retrieve._norm(first)
    ident_rows = [h["rows"] for h in lmap.values() if (h.get("rows") or {}).get("kind") == "identifier"]
    said_items = [n for n in items if len(n) >= 4 and n in low and (ident_rows or n != subj)]
    said_comps = [n for n in comps if len(n) >= 4 and n in low and n != subj]
    if first and ident_rows and said_items:  # "who sends X": the sentence must name the extracted sender/receiver
        sigs = [r for rows in ident_rows for r in rows.get("signals") or []]
        senders = {retrieve._norm(r["sender"]) for r in sigs if r.get("sender")}
        receivers = {retrieve._norm(x) for r in sigs for x in r.get("receivers") or []}
        bad = ((_SEND_V.search(first) and senders and not any(x in low for x in senders))
               or (_RECV_V.search(first) and receivers and not any(x in low for x in receivers)))
        return "", (_CONFLICT_NOTE if bad else "")
    if first and (said_items or said_comps):  # restates the list or the connections: the inventory list is used
        listed_n = retrieve._norm(listed)
        return "", ("" if all(n in listed_n for n in said_items) else _CONFLICT_NOTE)
    checked, _, dropped = supported_only(first, text_hits)
    return checked, "An optional description failed evidence checks and was removed." if dropped else ""


# ---------------------------------------------------------------- change-history answers (deterministic)
_LATEST_ENTRY = re.compile(r"\b(latest|last|newest|most recent|current)\b[^?]*\b(entry|row|change|revision|version)\b",
                           re.I)


def _date_key(d: str) -> tuple:
    m = re.match(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", d or "")
    if m:
        return int(m.group(3)), int(m.group(2)), int(m.group(1))
    m = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})", d or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


_BULLETS = re.compile(r"[•¾]\s*")


def _rev_text(r: dict) -> str:
    return (f"{r.get('date') or 'no date'}, document version {r.get('version') or '?'}, "
            f"{r.get('changed_by') or 'author not stated'}: {_BULLETS.sub('', r.get('description') or '').strip()}")


def history_answer(question: str, info: "retrieve.QueryInfo", lmap: dict[str, dict],
                   c: sqlite3.Connection, project_id: str) -> tuple[str, list[str], str] | None:
    """(text, labels, note) answered from the parsed change-history tables of the documents, for 'what changed
    between releases' and 'latest change history entry' questions; None when no change-history chunk of the
    document was retrieved (the LLM path then answers)."""
    if not (retrieve._HISTORY_Q.search(question) or info.qtype == "what_changed"):
        return None
    hist = {h["metadata"]["doc_id"]: lab for lab, h in lmap.items()
            if not h["metadata"].get("inventory") and retrieve._CHANGE_HISTORY.search(h["text"])}
    if not hist:
        return None
    docs = retrieve.project_docs(project_id, c)
    fam_of = {d["doc_id"]: d["family"] for d in docs}
    family = fam_of.get(next(iter(hist)))
    rel = sorted([d for d in docs if d["family"] == family], key=lambda d: retrieve.version_tuple(d.get("version")))
    revs = {}
    for d in rel:
        row = c.execute("SELECT revisions_json FROM documents WHERE project_id=? AND doc_id=?",
                        (project_id, d["doc_id"])).fetchone()
        revs[d["doc_id"]] = sorted(json.loads((row[0] if row else None) or "[]"), key=lambda r: _date_key(r.get("date")),
                                   reverse=True)
    named = [d for d in rel if d["doc_id"] in info.doc_ids]
    note = "Answered from the change-history table of the document; changes not recorded there are not covered."
    if info.qtype == "what_changed" and len(rel) >= 2:
        old, new = (named[0], named[-1]) if len(named) >= 2 else (rel[0], rel[-1])
        if new["doc_id"] not in hist:
            return None
        seen = {_rev_text(r) for r in revs[old["doc_id"]]}
        added = [r for r in revs[new["doc_id"]] if _rev_text(r) not in seen]
        lab = hist[new["doc_id"]]
        labs = [lab] + ([hist[old["doc_id"]]] if old["doc_id"] in hist else [])
        if not added:
            return (f"The change-history tables of release {old['version']} ({old['doc_id']}) and release "
                    f"{new['version']} ({new['doc_id']}) list the same entries [{', '.join(labs)}].", labs, note)
        rows = "; ".join(_rev_text(r) for r in added)
        return (f"The change history of release {new['version']} ({new['doc_id']}) has these entries that release "
                f"{old['version']} ({old['doc_id']}) does not have: {rows} [{', '.join(labs)}].", labs,
                note + " Use Compare for a section-by-section diff of the content.")
    pick = (named[-1] if named else rel[0] if info.version_pref == "older" else rel[-1]) if rel else None
    if not pick or pick["doc_id"] not in hist or not revs.get(pick["doc_id"]):
        return None
    lab, rows = hist[pick["doc_id"]], revs[pick["doc_id"]]
    which = ("the release named in the question" if named else "the oldest release" if info.version_pref == "older"
             else "the newest release")
    if _LATEST_ENTRY.search(question):
        return (f"The most recent change-history entry of release {pick['version']} ({pick['doc_id']}, {which}) is: "
                f"{_rev_text(rows[0])} [{lab}].", [lab], note)
    shown = "; ".join(_rev_text(r) for r in rows[:5])
    more = f" ({len(rows) - 5} older entries not shown)" if len(rows) > 5 else ""
    return (f"The change history of release {pick['version']} ({pick['doc_id']}, {which}) lists, newest first: "
            f"{shown}{more} [{lab}].", [lab], note)


# ---------------------------------------------------------------- release comparison answers (deterministic)
_BETWEEN_RELEASES = re.compile(r"\bbetween\b[^?]*\b(?:releases|versions)\b", re.I)
_DIFF_ITEMS = 8   # entries listed per kind and change; the rest is counted
_DIFF_TYPES = (("components", "Components"), ("ports", "Ports"), ("interfaces", "Interfaces"), ("signals", "Signals"))


def _release_label(d: dict) -> str:
    return f"{d.get('version') or '?'} ({d['doc_id']})"


def _release_list(docs: list[dict]) -> str:
    return ", ".join(_release_label(d) for d in sorted(docs, key=lambda d: retrieve.version_tuple(d.get("version"))))


def _pick_release_pair(info: "retrieve.QueryInfo", docs: list[dict], question: str):
    """(old, new) documents for a 'what changed' question, ('missing', [versions]) for named releases the project
    does not have, or None when the question does not pin two releases of one document."""
    named = list(dict.fromkeys(info.versions))
    versioned = [d for d in docs if d.get("version")]
    missing = [v for v in named if not any(retrieve.version_matches(v, d["version"]) for d in versioned)]
    if missing:
        return "missing", missing
    fams: dict[str, list[dict]] = {}
    for d in versioned:
        fams.setdefault(d["family"], []).append(d)
    for fam in fams.values():
        fam.sort(key=lambda d: retrieve.version_tuple(d["version"]))
    match = lambda fam, v: [d for d in fam if retrieve.version_matches(v, d["version"])]  # noqa: E731
    if len(named) >= 2:
        for fam in fams.values():
            a, b = (match(fam, v) for v in named[:2])
            if a and b and a[0] is not b[0]:
                return tuple(sorted((a[0], b[0]), key=lambda d: retrieve.version_tuple(d["version"])))
    elif len(named) == 1:
        for fam in fams.values():
            hit = match(fam, named[0])
            if hit and fam.index(hit[0]) > 0:  # compared with the release before it
                return fam[fam.index(hit[0]) - 1], hit[0]
    elif len(fams) == 1 and len(next(iter(fams.values()))) == 2 and _BETWEEN_RELEASES.search(question):
        return tuple(next(iter(fams.values())))
    return None


def _short(v: Any, n: int = 70) -> str:
    s = ", ".join(map(str, v)) if isinstance(v, list) else str(v if v not in (None, "") else "not stated")
    return s if len(s) <= n else s[:n - 3] + "..."


def release_diff_answer(question: str, info: "retrieve.QueryInfo", lmap: dict[str, dict], c: sqlite3.Connection,
                        project_id: str, base: dict) -> "Answer | None":
    """Answer 'what changed between two releases' without the similarity threshold: the added, removed and changed
    components, ports, interfaces and signals of ``compare.compare`` plus the change-history rows the newer release
    has and the older one lacks, cited to release + page. A named release the project lacks is refused with the list
    of releases that exist. None when the question does not pin two releases of one document (the normal path runs)."""
    from app import compare
    if info.qtype != "what_changed":
        return None
    docs = retrieve.project_docs(project_id, c)
    pair = _pick_release_pair(info, docs, question)
    if pair is None:
        return None
    if pair[0] == "missing":
        names = ", ".join(pair[1])
        have = _release_list(docs) or "none"
        return Answer(answer=f"Release {names} does not exist in project {project_id}. Releases that exist in this "
                             f"project: {have}.", confidence="Low", mode="refused", refusal_reason="not_found",
                      evidence=[], limitation="No comparison was made: a release named in the question is not in "
                                              "this project.", **base)
    old, new = pair
    try:
        diff = compare.compare(project_id, old["doc_id"], None, new["doc_id"], None, conn=c)
    except ValueError:
        return None
    cites: dict[tuple, str] = {}
    notes: dict[str, list[str]] = {}
    meta: dict[str, dict] = {}

    def cite(entry_cits, what: str) -> list[str]:
        out = []
        for x in entry_cits:
            key = (x.doc_id, x.page, x.section)
            if key not in cites:
                cites[key] = f"C{len(cites) + 1}"
                meta[cites[key]] = {"doc_id": x.doc_id, "version": x.version, "page_start": x.page, "page_end": x.page,
                                    "section_id": x.section.split(" ", 1)[0] if x.section else None,
                                    "section_title": x.section.split(" ", 1)[1] if " " in x.section else ""}
            lab = cites[key]
            notes.setdefault(lab, []).append(what)
            if lab not in out:
                out.append(lab)
        return out

    def tag(labs: list[str]) -> str:
        return f" [{', '.join(labs)}]" if labs else ""

    parts = [f"Compared the extracted inventories of release {_release_label(old)} and release {_release_label(new)}."]
    total = 0
    for attr, title in _DIFF_TYPES:
        td = getattr(diff, attr)
        clauses = []
        for kind, entries in (("added", td.added), ("removed", td.removed), ("changed", td.changed)):
            if not entries:
                continue
            total += len(entries)
            labs: list[str] = []
            shown = []
            for e in entries[:_DIFF_ITEMS]:
                what = f"{kind} in {title.lower()}: {e.name}"
                labs += [x for x in cite(e.citations, what) if x not in labs]
                shown.append(e.name if kind != "changed" else f"{e.name} ("
                             + "; ".join(f"{f}: {_short(v['old'])} -> {_short(v['new'])}" for f, v in e.changes.items())
                             + ")")
            more = f" and {len(entries) - _DIFF_ITEMS} more" if len(entries) > _DIFF_ITEMS else ""
            clauses.append(f"{len(entries)} {kind} ({'; '.join(shown)}{more}){tag(labs)}")
        if clauses:
            parts.append(f"{title}: " + ", ".join(clauses) + ".")
    if diff.sections:
        for kind, secs in (("added", diff.sections.added), ("removed", diff.sections.removed),
                           ("modified", diff.sections.modified)):
            for s in secs[:_DIFF_ITEMS]:
                total += 1
                pg, doc = (s.pages_b, new) if s.pages_b else (s.pages_a, old)
                sec_no = s.section_b or s.section_a or ""
                labs = cite([compare.Citation(doc_id=doc["doc_id"], version=doc.get("version"),
                                              page=pg[0] if pg else None, section=f"{sec_no} {s.title}".strip())],
                            f"text section {kind}: {s.title}")
                parts.append(f"Section {sec_no} {s.title} was {kind}{tag(labs)}.")
    hist_hit = next(((lab, h) for lab, h in lmap.items()
                     if h["metadata"].get("doc_id") == new["doc_id"] and not h["metadata"].get("inventory")
                     and retrieve._CHANGE_HISTORY.search(h["text"])), None)
    seen = {_rev_text(r) for r in diff.revisions_a}
    rows = [_rev_text(r) for r in diff.revisions_b if _rev_text(r) not in seen]
    if rows and hist_hit:
        m = hist_hit[1]["metadata"]
        labs = cite([compare.Citation(doc_id=new["doc_id"], version=m.get("version"), page=m.get("page_start"),
                                      section=f"{m.get('section_id') or ''} {m.get('section_title') or ''}".strip())],
                    "change-history table of the newer release: " + "; ".join(rows))
        parts.append(f"The change history of release {_release_label(new)} has entries that release "
                     f"{_release_label(old)} does not have: {'; '.join(rows)}{tag(labs)}.")
    elif rows:
        parts.append(f"The change history of release {_release_label(new)} has entries that release "
                     f"{_release_label(old)} does not have: {'; '.join(rows)}.")
    if total == 0 and not rows:
        parts.append("No difference was found between the extracted inventories or the section texts of the two "
                     "releases.")
    labels = sorted(meta, key=lambda x: int(x[1:]))
    citations = [Citation(label=lab, **meta[lab]) for lab in labels]
    evidence = [{"label": lab, "text": "; ".join(dict.fromkeys(notes[lab])), **meta[lab]} for lab in labels]
    return Answer(answer=" ".join(parts), confidence="Medium", mode="generated", citations=citations,
                  evidence=evidence, built_from="inventory", answer_type="text",
                  confidence_reason="difference of the extracted inventories of the two releases, not checked "
                                    "against the pages",
                  limitation="Built from the extracted inventories of both releases (deterministic, no similarity "
                             "threshold); changes in text that was not extracted are not listed. Check them against "
                             "the cited pages. Use Compare for the full section-by-section diff.", **base)


def release_difference_note(lmap: dict[str, dict], labels: list[str]) -> str:
    """When a cited chunk replaced the equivalent chunk of an older release (version policy) and their texts differ,
    say that the releases differ there ('' otherwise)."""
    corpus_texts = {}
    notes = []
    for lab in labels:
        h = lmap.get(lab) or {}
        old_id = h.get("replaced")
        if not old_id:
            continue
        old_doc = old_id.split("::", 1)[0]
        corpus_texts.setdefault(old_id, h.get("replaced_text", ""))
        if corpus_texts[old_id] and retrieve._jaccard(corpus_texts[old_id], h["text"]) < 0.85:
            notes.append(f"{lab} ({h['metadata'].get('doc_id')}) differs from the same section in {old_doc}")
    return ("The releases differ here: " + "; ".join(notes) + ".") if notes else ""


OFFTOPIC_COVERAGE = 0.5  # set on the dev set: answerable >= 0.8, on-topic unanswerable 0.5-0.75, off-topic 0.33


def _searched_note(r: "retrieve.Retrieval") -> str:
    """'Not found in <docs>' for an on-topic question the documents do not answer (what was searched)."""
    docs = list(dict.fromkeys(f"{h['metadata'].get('doc_id')} ({h['metadata'].get('version')})" for h in r.hits))
    where = ", ".join(docs[:3]) or ", ".join(r.searched[:4]) or "the project's documents"
    miss = f" Terms not found anywhere in the project: {', '.join(r.missing_terms[:6])}." if r.missing_terms else ""
    return (f"Not found in {where} (searched {len(r.searched) or 'all'} document(s) of this project).{miss}")


def gap_answer(lmap: dict[str, dict]) -> tuple[str, list[str]] | None:
    """(text, labels) when the question names a component whose inventory has no ports/signals (an
    "extraction gap" chunk was retrieved): says so plainly, names a composition's children, and cites the
    component's own section and the closest retrieved text sections. None when there is no gap chunk."""
    gaps = [(lab, h) for lab, h in lmap.items() if h["metadata"].get("extraction_gap")]
    if not gaps:
        return None
    texts = sorted(((lab, h) for lab, h in lmap.items() if not h["metadata"].get("inventory")),
                   key=lambda x: -x[1]["score"])
    sents, labels = [], []
    for lab, h in gaps:
        m, rows = h["metadata"], h.get("rows") or {}
        comp = m["extraction_gap"]
        sents.append(f"No ports were extracted for {comp} in {m.get('doc_id')}; this may be a gap in extraction.")
        if rows.get("children"):
            sents.append(f"{comp} is a software composition containing {', '.join(rows['children'])} [{lab}].")
            labels.append(lab)
        closest, seen = [], set()
        if rows.get("section") and m.get("page_start"):
            closest.append((lab, f"§{rows['section']}, p.{m['page_start']}"))
            seen.add((m.get("doc_id"), m.get("section_id")))
        for tl, th in texts:
            tm = th["metadata"]
            key = (tm.get("doc_id"), tm.get("section_id"))
            if key in seen or len(closest) >= 3:
                continue
            seen.add(key)
            closest.append((tl, f"§{tm.get('section_id')} {tm.get('section_title') or ''}".rstrip()
                            + f", p.{tm.get('page_start')}"))
        if closest:  # a label already cited (the composition sentence) is not repeated
            sents.append("Closest sections: " + "; ".join(t if l in labels else f"{t} [{l}]" for l, t in closest) + ".")
            labels += [l for l, _ in closest if l not in labels]
    return " ".join(sents), labels


def _gap_refusal(gap: tuple[str, list[str]], lmap: dict, evidence: list, base: dict) -> Answer:
    """Refusal (reason "not_extracted") that says no ports were extracted for the named component, with citations."""
    text, labels = gap
    cites = [_citation(lab, lmap[lab]) for lab in labels]
    return Answer(answer=text, confidence="Low", mode="refused", refusal_reason="not_extracted", citations=cites,
                  evidence=evidence, built_from="inventory", confidence_reason="nothing was extracted to answer from",
                  limitation="The inventory has no ports or signals for this component; the document may still "
                             "describe them (for example in a figure stored as an image). Check the cited sections.",
                  **base)


def _answer(project_id: str, question: str, user: str, k: int, c: sqlite3.Connection, chroma_dir, provider,
            threshold: float | None, model: str | None = None,
            retrieval: retrieve.RetrievalConfig | None = None) -> Answer:
    thr = settings.similarity_threshold if threshold is None else threshold
    if retrieve.embedder_status()["fallback"] and os.environ.get("ALLOW_HASH_QA") != "1":
        # similarity threshold / HIGH_SCORE were tuned for bge; hashing scores are not comparable -> do not answer
        return Answer(answer=REFUSAL_TEXT, confidence="Low", mode="refused", evidence=[],
                      limitation="Embedder fallback active (hashing embedder instead of bge): similarity thresholds "
                                 "are not valid, so threshold-based answering is disabled. Install/load the "
                                 "embedding model and re-ingest.", project_id=project_id, question=question)
    r = retrieve.retrieve(project_id, question, k=k, cfg=retrieval, conn=c, chroma_dir=chroma_dir)
    hits, top = r.hits, r.top_score
    base = {"project_id": project_id, "question": question}
    section = re.fullmatch(r"\s*(?:what does|describe|summari[sz]e|explain)\s+section\s+"
                           r"(\d+(?:\.\d+)*)(?:\s+describe)?\s*[?.]?\s*", question, re.I)
    if section and r.section_match:
        lmap = label_map(hits)
        selected = {}
        for label, hit in lmap.items():
            meta = hit["metadata"]
            if str(meta.get("section_id", "")) == section[1] and not meta.get("inventory"):
                selected.setdefault(meta["doc_id"], (label, hit))
        if selected:
            lines, citations, evidence = [], [], []
            for doc_id, (label, hit) in selected.items():
                meta = hit["metadata"]
                excerpt = re.sub(r"\s+", " ", hit["text"]).strip()
                if len(excerpt) > 500:
                    excerpt = excerpt[:500].rsplit(" ", 1)[0] + "..."
                lines.append(f"Section {section[1]} in {doc_id} ({meta.get('section_title', '')}): "
                             f"{excerpt} [{label}]")
                citations.append(_citation(label, hit))
                evidence.append({"label": label, "text": hit["text"], "score": hit["score"], **meta})
            return Answer(answer="\n\n".join(lines), mode="generated", confidence="Medium",
                          confidence_reason="exact section-number lookup; source excerpts shown directly",
                          built_from="section_lookup", citations=citations, evidence=evidence,
                          limitation="Direct section excerpts, not a model-written summary. The same section "
                                      "number can occur in several project documents.", **base)
    if os.environ.get("QA_DIFF", "1") != "0":  # releases named in the question: no similarity-threshold refusal
        diffed = release_diff_answer(question, r.query, label_map(hits), c, project_id, base)
        if diffed:
            return diffed
    # on-topic below the threshold: a component named in the question matched the inventory, or an identifier from
    # the question occurs verbatim in a retrieved chunk and the score is within IDENTIFIER_MARGIN of the threshold
    on_topic = top >= thr or r.on_topic or (r.exact_identifier and top >= thr - IDENTIFIER_MARGIN)
    named = r.on_topic or r.exact_identifier or bool(r.query.components)
    if hits and not named and r.coverage < OFFTOPIC_COVERAGE and os.environ.get("QA_OFFTOPIC", "1") != "0":
        return Answer(answer=REFUSAL_TEXT, confidence="Low", mode="refused", evidence=[], refusal_reason="off_topic",
                      limitation=f"Off-topic: most of the question's terms ({', '.join(r.missing_terms[:6])}) do not "
                                 "occur in this project's documents, and it names no known component or identifier.",
                      **base)
    if not hits or not on_topic:
        return Answer(answer=REFUSAL_TEXT, confidence="Low", mode="refused", evidence=[],
                      refusal_reason="not_found" if r.coverage >= OFFTOPIC_COVERAGE else "off_topic",
                      limitation=f"Best retrieval score {top:.2f} is below the threshold {thr:.2f}; "
                                 "the question may be outside the documents. " + _searched_note(r), **base)
    lmap = label_map(hits)
    evidence = [{"label": lab, "text": h["text"], "score": round(h["score"], 4), **h["metadata"],
                 **(_inventory_summary(h) if h["metadata"].get("inventory") else {})} for lab, h in lmap.items()]
    provider = provider or llm.get_provider()
    struct = structured_blocks(question, r.query, lmap) if os.environ.get("QA_STRUCTURED", "1") != "0" else None
    if struct:
        text, labels, blocks, rows = struct
        subject = next((h["rows"].get("component") or h["rows"].get("identifier") for lab, h in lmap.items()
                        if lab in labels), "")
        desc, note = _description(provider, question, subject, lmap, model, listed=text)
        if desc:
            text += " " + desc
            labels += [x for x in parse_citations(desc, lmap) if x not in labels]
        cites = [_citation(lab, lmap[lab]) for lab in labels]
        limitation = " ".join(x for x in ("Lists built from the extracted inventory (deterministic); check them "
                                          "against the cited pages.", note) if x)
        used = {x.doc_id for x in cites}
        if r.release_note and any(f"({d})" in r.release_note for d in used):
            limitation += " " + r.release_note
        nouns = {b["noun"] for b in blocks}
        conf, why = inventory_confidence(rows, lmap, nouns.pop() if len(nouns) == 1 else "rows")
        return Answer(answer=text, confidence=conf, confidence_reason=why, mode="generated", citations=cites,
                      evidence=evidence, limitation=_ocr_limitation(limitation, cites), answer_type="inventory_list",
                      built_from="inventory+llm" if desc else "inventory",
                      inventory={"blocks": blocks, "description": desc}, **base)
    hist = history_answer(question, r.query, lmap, c, project_id) if os.environ.get("QA_HISTORY", "1") != "0" else None
    if hist:
        text, labels, note = hist
        cites = [_citation(lab, lmap[lab]) for lab in labels]
        return Answer(answer=text, confidence="High", mode="generated", citations=cites, evidence=evidence,
                      limitation=note, built_from="change_history",
                      confidence_reason="copied from the document's change-history table", **base)
    gap = gap_answer(lmap) if os.environ.get("QA_GAP", "1") != "0" else None
    prompt, system = build_prompt(question, hits)
    try:
        text = _generate(provider, prompt, system, model, [h["id"] for h in hits])
        text = text if isinstance(text, str) else json.dumps(text)
    except (llm.LLMNotConfigured, NotImplementedError):
        if gap:
            return _gap_refusal(gap, lmap, evidence, base)
        cites = [_citation(lab, h) for lab, h in lmap.items()]
        return Answer(answer=EVIDENCE_ONLY_TEXT, confidence="Low", mode="evidence_only", citations=cites,
                      evidence=evidence, limitation=_ocr_limitation(
                          ("No LLM configured: these are the top retrieved passages, not a synthesised answer. "
                           + r.release_note).strip(), cites), **base)
    clean, valid, share = postprocess(text, lmap)
    clean, valid, unsupported = supported_only(clean, lmap)
    if valid and all(NOT_STATED.lower() in x.lower() for x in split_sentences(clean)):
        valid = []  # only "Not stated ..." sentences: an absence statement, not an answer
    if gap and (not valid or _NEGATION.search(clean)):
        return _gap_refusal(gap, lmap, evidence, base)  # instead of refusing, or claiming it has no ports
    if not valid:
        return Answer(answer=REFUSAL_TEXT, confidence="Low", mode="refused", evidence=evidence,
                      refusal_reason="not_found",
                      limitation="The model produced no sentence passing the citation and evidence checks against the retrieved "
                                 "evidence, so the answer was withheld. " + _searched_note(r), **base)
    cites = [_citation(lab, lmap[lab]) for lab in valid]
    support = evidence_support(clean, lmap) if os.environ.get("QA_SUPPORT", "1") != "0" else None
    conf = compute_confidence("generated", top, len(valid), share, support)
    limitation = ("Some cited sentences share few terms or numbers with their cited chunk; check them. "
                  if support and support["supported"] < support["claims"] else "")
    limitation = (limitation + ("Some uncited sentences were removed." if share < 1.0 else "")).strip()
    if unsupported:
        limitation = (limitation + " Some cited claims failed evidence checks and were removed.").strip()
    if NOT_STATED.lower() in clean.lower():
        limitation = (limitation + " Part of the question is not stated in the documents.").strip()
    for gap in dict.fromkeys(h["metadata"]["extraction_gap"] for h in hits if h["metadata"].get("extraction_gap")):
        limitation = (limitation + f" No ports were extracted for {gap} (possible extraction gap); this does not "
                                   f"mean that {gap} has no ports.").strip()
    used = {x.doc_id for x in cites}
    if r.release_note and any(f"({d})" in r.release_note for d in used):
        limitation = (limitation + " " + r.release_note).strip()
    limitation = (limitation + " " + release_difference_note(lmap, valid)).strip()
    return Answer(answer=clean, confidence=conf, mode="generated", citations=cites, evidence=evidence,
                  limitation=_ocr_limitation(limitation, cites), built_from="llm", **base)


def ask(project_id: str, question: str, user: str, k: int = 5, conn: sqlite3.Connection | None = None,
        chroma_dir=None, provider=None, threshold: float | None = None, model: str | None = None,
        retrieval: retrieve.RetrievalConfig | None = None, store: bool = True) -> Answer:
    """Answer a question from the project's indexed documents. Raises PermissionDenied for non-members.
    ``retrieval`` selects the retrieval stages (default: settings; ``RetrievalConfig.dense_only()`` = old).
    ``store=False`` (the Ask page checking its sample questions) keeps no answer row and audits "ask_check"."""
    with controls._use(conn) as c:
        controls.require_role(user, project_id, "viewer", c)
        ans = _answer(project_id, question, user, k, c, chroma_dir, provider, threshold, model, retrieval)
        if ans.mode == "generated" and ans.built_from in ("llm", "inventory+llm"):
            ans.model = model or getattr(provider, "model", "") or ""
        if store:
            _store(c, ans, user)
        else:
            controls.log_action(user, "ask_check", project_id, {"mode": ans.mode, "question": question[:200]}, c)
    return ans
