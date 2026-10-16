"""Controlled PDF ingestion: validation, parsing (sections, tables, revisions), storage, indexing.

Processed JSON shape (data/processed/{project_id}/{doc_id}.json)::

    {doc_id, project_id, title, doc_type, version, source_status, sha256, n_pages,
     metadata: {...page-1 key/value pairs...},
     revisions: [{date, version, changed_by, description}],
     sections: [{section_id, title, level, parent_id, page_start, page_end, text,
                 tables: [{table_id, kind, page_start, page_end, header, rows}],
                 ocr?: true, ocr_confidence?: float}],   # set on sections containing OCR text
     ocr_pages: [{page, status, confidence, chars}]}      # scanned pages (status ok|tesseract_missing|...)

Section "0" ("Front matter") holds pages 1-4 / pre-heading content. ``kind`` of a table is one of
``change_history``, ``table`` (recognised header row) or ``key_value``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

import pymupdf

from app import controls, headers, ocr
from app.config import settings

log = logging.getLogger(__name__)

HEAD_RE = re.compile(r"^(\d+(?:\.\d+){0,3})\s+(\S.*)$")
BARE_RE = re.compile(r"^\d+(?:\.\d+){0,5}$")
FOOTER_RES = [
    re.compile(r"^\d+ of \d+$"),
    re.compile(r"^Document ID \d+: (?:AUTOSAR|SYN)_\w+$"),
    re.compile(r"^- ?AUTOSAR CONFIDENTIAL- ?$"),
]
LEADER_RE = re.compile(r"\.{4,}|(\. ){4,}")
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})$")
BULLETS = ("•", "¾", "-", "●")
BODY_START_PAGE = 5
HEADER_TOKENS = {
    "port", "interface", "component", "signal", "date", "version", "release", "name", "direction",
    "kind", "type", "description", "sender", "receiver", "receivers", "id", "text", "feature",
    "abbreviation", "acronym", "comments", "swc", "operation", "data", "element", "seat", "axis",
    "diagram", "reference", "changed", "by", "elements", "composition", "category",
}


class IngestRejected(Exception):
    """Raised when an upload is rejected by policy (and the rejection is audit-logged)."""


# ---------------------------------------------------------------- text helpers
def _norm(s: str | None) -> str:
    s = (s or "").replace("△", "").replace("�", "'").replace("­", "")
    return re.sub(r"\s+", " ", s).strip()


def _join_lines(parts: list[str]) -> str:
    out = ""
    for p in parts:
        if not out:
            out = p
        elif out.endswith("-") and p[:1].islower():
            out = out[:-1] + p
        else:
            out += " " + p
    return out


def _is_footer(text: str) -> bool:
    return any(r.match(text) for r in FOOTER_RES)


# ---------------------------------------------------------------- line extraction
def _page_lines(page: "pymupdf.Page") -> list[dict[str, Any]]:
    """Text lines (with size/bold/bbox/block id) outside the header/footer zones."""
    h = page.rect.height
    lines: list[dict[str, Any]] = []
    for bi, b in enumerate(page.get_text("dict")["blocks"]):
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            x0, y0, x1, y1 = ln["bbox"]
            if y1 < 0.11 * h or y0 > 0.89 * h:
                continue
            text = _norm("".join(s["text"] for s in ln["spans"]))
            if not text or _is_footer(text):
                continue
            main = max(spans, key=lambda s: len(s["text"]))
            bold = bool(main["flags"] & 16) or "bold" in main["font"].lower()
            lines.append({"text": text, "size": round(main["size"], 1), "bold": bold,
                          "bbox": (x0, y0, x1, y1), "block": bi})
    return lines


# ---------------------------------------------------------------- tables
def _collapse(row: list[str | None]) -> list[str]:
    return [c for c in (_norm(x) for x in row) if c]


def _aligned_rows(raw: list[list[str | None]]) -> list[list[str]] | None:
    """Rectangular table whose header cells are all filled but some body cells are empty: keep the empty cells so
    values stay in their columns (collapsing would shift e.g. 'Type' into an empty 'Description'). None otherwise."""
    rows = [[_norm(c) for c in r] for r in raw]
    rows = [r for r in rows if any(r)]
    if (len(rows) >= 2 and len(rows[0]) >= 2 and all(rows[0]) and all(len(r) == len(rows[0]) for r in rows)
            and any(not all(r) for r in rows[1:])):
        return rows
    return None


def _header_valid(row: list[str]) -> bool:
    if len(row) < 2:
        return False
    for cell in row:
        toks = set(re.findall(r"[a-z]+", cell.lower()))
        if len(cell) > 40 or not (toks & HEADER_TOKENS or headers.match_header(cell)):
            return False
    return True


def _roles_ok(cr: list[list[str]]) -> bool:
    """Unknown header words, but the body cells clearly hold port/interface/kind data (value-pattern inference)."""
    roles = headers.infer_roles(cr[1:])
    return len(cr[0]) >= 3 and all(len(c) <= 40 for c in cr[0]) and (
        "direction" in roles and ("port" in roles or "interface" in roles) or {"port", "interface"} <= set(roles)
        or {"interface", "kind"} <= set(roles))


def _is_garbage(cr: list[list[str]], max_len: int = 300) -> bool:
    return any("[" in c or "::" in c or len(c) > max_len for r in cr for c in r)


def _classify_table(cr: list[list[str]], bbox: tuple, page_h: float, prev: dict | None,
                    page_no: int) -> tuple[str, list[str], list[list[str]], bool] | None:
    """Return (kind, header, rows, is_continuation) or None for non-tables."""
    first_two = [c for r in cr[:2] for c in r]
    if "Document Change History" in first_two:
        header = next((r for r in cr if r and r[0].lower() == "date"), [])
        rows = [r for r in cr if r and DATE_RE.match(r[0])]
        return "change_history", header, rows, False
    if cr and DATE_RE.match(cr[0][0]) and prev and prev["kind"] == "change_history":
        return "change_history", [], [r for r in cr if r], True
    if _is_garbage(cr, 2000) or len(cr) < 2:
        return None
    width = max(len(r) for r in cr)
    if width < 2:
        return None
    if (_header_valid(cr[0]) or _roles_ok(cr)) and not _is_garbage(cr):
        return "table", cr[0], cr[1:], False
    near_top = bbox[1] < 0.25 * page_h
    if (prev and near_top and prev["page_end"] == page_no - 1 and prev["kind"] != "change_history"
            and len(prev["rows"][-1]) == width and all(len(r) == width for r in cr)):
        return prev["kind"], prev["header"], cr, True
    if all(len(r) == 2 for r in cr[1:]) and len(cr[0]) in (1, 2):
        return "key_value", [], cr, False
    return None


def _page_tables(plpage: Any, page_no: int, page_h: float, prev: dict | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        found = plpage.find_tables()
    except Exception as e:  # pdfplumber can choke on odd pages
        log.warning("find_tables failed on page %s: %s", page_no, e)
        return out
    for t in sorted(found, key=lambda t: t.bbox[1]):
        raw = t.extract()
        cr = _aligned_rows(raw) or [r for r in (_collapse(row) for row in raw) if r]
        res = _classify_table(cr, t.bbox, page_h, prev, page_no)
        if res is None:
            continue
        kind, header, rows, cont = res
        if cont and prev is not None:
            if "row_pages" not in prev:
                prev["row_pages"] = [prev["page_start"]] * len(prev["rows"])
            prev["rows"].extend(rows)
            prev["row_pages"].extend([page_no] * len(rows))
            prev["page_end"] = page_no
            out.append({"merged": True, "bbox": t.bbox})
            continue
        tab = {"kind": kind, "page_start": page_no, "page_end": page_no, "header": header,
               "rows": rows, "row_pages": [page_no] * len(rows), "bbox": t.bbox}
        out.append(tab)
        prev = tab
    return out


# ---------------------------------------------------------------- parsing
def _detect_heading(lines: list[dict], i: int, body_ok: bool) -> tuple[str, str, int] | None:
    """Return (number, title, last_line_index) if lines[i] starts a numbered heading."""
    L = lines[i]
    if not body_ok or not L["bold"] or L["size"] < 12 or "...." in L["text"]:
        return None
    last = i
    m = HEAD_RE.match(L["text"])
    if m:
        number, title = m.group(1), m.group(2)
    elif BARE_RE.match(L["text"]) and i + 1 < len(lines):
        nxt = lines[i + 1]
        if not (nxt["bold"] and abs(nxt["size"] - L["size"]) < 0.5) or HEAD_RE.match(nxt["text"]):
            return None
        number, title, last = L["text"], nxt["text"], i + 1
    else:
        return None
    while last + 1 < len(lines):
        n, p = lines[last + 1], lines[last]
        if (n["bold"] and abs(n["size"] - p["size"]) < 0.3 and not HEAD_RE.match(n["text"])
                and not BARE_RE.match(n["text"]) and n["bbox"][1] - p["bbox"][3] < 0.6 * p["size"]):
            title = _join_lines([title, n["text"]])
            last += 1
        else:
            break
    return number, title, last


def _in_bbox(line: dict, bboxes: list[tuple]) -> bool:
    cx = (line["bbox"][0] + line["bbox"][2]) / 2
    cy = (line["bbox"][1] + line["bbox"][3]) / 2
    return any(b[0] - 1 <= cx <= b[2] + 1 and b[1] - 1 <= cy <= b[3] + 1 for b in bboxes)


class _Builder:
    """Accumulates sections while pages are streamed in order."""

    def __init__(self, doc_id: str) -> None:
        self.doc_id = doc_id
        self.sections: list[dict[str, Any]] = []
        self._paras: list[list[str]] = []
        self._last_block: tuple[int, int] | None = None
        self.tcount = 0
        self.open_section("0", "Front matter", 0, 1)

    def open_section(self, sid: str, title: str, level: int, page: int) -> None:
        self._flush()
        parent = sid.rsplit(".", 1)[0] if "." in sid else ("0" if sid != "0" else None)
        self.sections.append({"section_id": sid, "title": title, "level": level, "parent_id": parent,
                              "page_start": page, "page_end": page, "text": "", "tables": [], "page_texts": {}, "_oc": []})
        self._paras = []
        self._last_block = None

    def add_line(self, line: dict, page: int) -> None:
        key = (page, line["block"])
        starts_bullet = line["text"][:1] in BULLETS[:2]
        if key != self._last_block or starts_bullet or not self._paras:
            self._paras.append([])
        self._paras[-1].append(line["text"])
        pages = self.sections[-1]["page_texts"]
        pages[str(page)] = (pages.get(str(page), "") + "\n" + line["text"]).strip()
        self._last_block = key
        if "ocr_conf" in line:
            self.sections[-1]["_oc"].append(line["ocr_conf"])
        self.sections[-1]["page_end"] = max(self.sections[-1]["page_end"], page)

    def add_table(self, tab: dict) -> None:
        self._flush()
        self.tcount += 1
        sec = self.sections[-1]
        sec["tables"].append({"table_id": f"{self.doc_id}-t{self.tcount}", "kind": tab["kind"],
                              "page_start": tab["page_start"], "page_end": tab["page_end"],
                              "header": tab["header"], "rows": tab["rows"], "_live": tab})
        sec["page_end"] = max(sec["page_end"], tab["page_end"])
        self._last_block = None

    def _flush(self) -> None:
        if not self.sections or not self._paras:
            return
        paras = [_join_lines(p) for p in self._paras if p]
        sec = self.sections[-1]
        sec["text"] = (sec["text"] + "\n" + "\n".join(paras)).strip("\n")
        self._paras = []

    def finish(self) -> list[dict[str, Any]]:
        self._flush()
        for s in self.sections:
            oc = s.pop("_oc")
            if oc:
                s["ocr"], s["ocr_confidence"] = True, round(sum(oc) / len(oc), 1)
            for t in s["tables"]:
                live = t.pop("_live")  # continuation rows were appended to the live dict
                t["rows"], t["page_end"] = live["rows"], live["page_end"]
                t["row_pages"] = live.get("row_pages", [t["page_start"]] * len(t["rows"]))
                s["page_end"] = max(s["page_end"], t["page_end"])
        return self.sections


def _has_toc(pdoc: "pymupdf.Document", scan: int = 10) -> bool:
    """True when one of the first pages looks like a table of contents (>= 3 dotted-leader lines)."""
    for idx in range(min(scan, pdoc.page_count)):
        if sum(1 for ln in _page_lines(pdoc[idx]) if LEADER_RE.search(ln["text"])) >= 3:
            return True
    return False


def parse_pdf(path: Path | str, doc_id: str) -> dict[str, Any]:
    """Parse a PDF into sections/tables/revisions/metadata (no storage side effects)."""
    import pdfplumber

    builder = _Builder(doc_id)
    last_table: dict | None = None
    toc_seen = False
    all_tables: list[dict] = []
    ocr_pages: list[dict[str, Any]] = []
    with pymupdf.open(str(path)) as pdoc, pdfplumber.open(str(path)) as plumb:
        n_pages = pdoc.page_count
        # documents without a table-of-contents page start their numbered headings on page 1 (no 4-page preamble)
        body_from = BODY_START_PAGE if _has_toc(pdoc) else 1
        for idx in range(n_pages):
            page_no = idx + 1
            page = pdoc[idx]
            lines = _page_lines(page)
            if not lines and ocr.is_scanned(page):
                o = ocr.ocr_page(page)
                ocr_pages.append({"page": page_no, "status": o["status"], "confidence": o["confidence"],
                                  "chars": o["chars"]})
                h = page.rect.height
                lines = [ln for ln in o["lines"] if not (ln["bbox"][3] < 0.11 * h or ln["bbox"][1] > 0.89 * h)
                         and not _is_footer(ln["text"])]
            if sum(1 for ln in lines if LEADER_RE.search(ln["text"])) >= 3:
                lines = []  # table-of-contents page
                toc_seen = True
            lines = [ln for ln in lines if "...." not in ln["text"]]
            tabs = _page_tables(plumb.pages[idx], page_no, page.rect.height, last_table)
            real = [t for t in tabs if not t.get("merged")]
            if real:
                last_table = real[-1]
            bboxes = [t["bbox"] for t in tabs]
            lines = [ln for ln in lines if not _in_bbox(ln, bboxes)]
            pending = sorted(real, key=lambda t: t["bbox"][1])
            i = 0
            while i < len(lines):
                while pending and pending[0]["bbox"][1] <= lines[i]["bbox"][1]:
                    builder.add_table(pending.pop(0))
                head = _detect_heading(lines, i, toc_seen or page_no >= body_from or "ocr_conf" in lines[i])
                if head:
                    number, title, last = head
                    builder.open_section(number, title, number.count(".") + 1, page_no)
                    i = last + 1
                    continue
                builder.add_line(lines[i], page_no)
                i += 1
            for t in pending:
                builder.add_table(t)
            all_tables.extend(real)
    sections = builder.finish()
    return {"doc_id": doc_id, "n_pages": n_pages, "sections": sections,
            "revisions": _revisions(sections), "metadata": _metadata(sections), "ocr_pages": ocr_pages}


def _revisions(sections: list[dict]) -> list[dict[str, str]]:
    revs: list[dict[str, str]] = []
    for s in sections:
        for t in s["tables"]:
            if t["kind"] != "change_history":
                continue
            for r in t["rows"]:
                if len(r) >= 3 and DATE_RE.match(r[0]):
                    revs.append({"date": r[0], "version": r[1], "changed_by": r[2],
                                 "description": " ".join(r[3:]).strip()})
    return revs


def _metadata(sections: list[dict]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for t in sections[0]["tables"]:
        if t["kind"] == "key_value":
            for r in t["rows"]:
                if len(r) == 2:
                    meta[r[0]] = r[1]
    return meta


# ---------------------------------------------------------------- storage
def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def processed_path(project_id: str, doc_id: str, processed_dir: Path | str | None = None) -> Path:
    base = Path(processed_dir) if processed_dir else settings.processed_dir
    return base / project_id / f"{doc_id}.json"


def load_processed(project_id: str, doc_id: str, processed_dir: Path | str | None = None) -> dict[str, Any]:
    """Load a processed document JSON (see module docstring for the shape)."""
    return json.loads(processed_path(project_id, doc_id, processed_dir).read_text(encoding="utf-8"))


def iter_tables(doc: dict[str, Any]):
    """Yield (section, table) pairs for every table in a processed document."""
    for s in doc["sections"]:
        for t in s["tables"]:
            yield s, t


_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ ()+-]{0,150}")


def _name_problem(name: str) -> str:
    """Why an uploaded file name is refused ('' when fine): no folders, no '..', plain characters only."""
    if "/" in name or "\\" in name or ".." in name:
        return f"invalid file name {name!r}: folders and '..' are not allowed"
    if not _SAFE_NAME.fullmatch(name):
        return f"invalid file name {name!r}: use letters, digits, space, . _ - ( ) +"
    return ""


def _reject(c: sqlite3.Connection, user: str, project_id: str, target: str, reason: str) -> None:
    controls.log_action(user, "ingest_rejected", f"{project_id}:{target}", reason, c)
    raise IngestRejected(reason)


def ingest_document(path: Path | str, project_id: str, doc_type: str, version: str,
                    source_status: str, user: str, *, doc_id: str | None = None,
                    title: str | None = None, conn: sqlite3.Connection | None = None,
                    processed_dir: Path | str | None = None, chroma_dir: Path | str | None = None,
                    index: bool = True, original_name: str | None = None) -> str:
    """Validate, parse, store and index a PDF. Returns the doc_id. Rejections are audit-logged.
    ``original_name``: the file name as sent by the client (checked before it is used as doc_id)."""
    path = Path(path)
    doc_id = doc_id or path.stem
    with controls._use(conn) as c:
        try:
            controls.require_role(user, project_id, "admin", c)
        except controls.PermissionDenied as e:
            controls.log_action(user, "ingest_denied", f"{project_id}:{path.name}", str(e), c)
            raise
        bad_name = _name_problem(original_name if original_name is not None else path.name)
        if bad_name:
            _reject(c, user, project_id, path.name, bad_name)
        if source_status not in controls.SOURCE_STATUSES:
            _reject(c, user, project_id, path.name, f"source_status {source_status!r} not allowed (approved/synthetic only)")
        if doc_type not in controls.DOC_TYPES:
            _reject(c, user, project_id, path.name, f"invalid doc_type {doc_type!r}")
        if path.suffix.lower() != ".pdf" or not path.is_file():
            _reject(c, user, project_id, path.name, "not a PDF file")
        size_mb = path.stat().st_size / 1e6
        if size_mb > settings.max_upload_mb:
            _reject(c, user, project_id, path.name,
                    f"file too large ({size_mb:.1f} MB > {settings.max_upload_mb:g} MB, MAX_UPLOAD_MB)")
        with open(path, "rb") as f:
            if not f.read(5).startswith(b"%PDF"):
                _reject(c, user, project_id, path.name, "file content is not a PDF")
        try:
            with pymupdf.open(str(path)) as probe:
                locked = probe.needs_pass or probe.is_encrypted
        except Exception:  # noqa: BLE001 - unreadable files are rejected by parse_pdf below
            locked = False
        if locked:
            _reject(c, user, project_id, path.name, "password-protected PDF: remove the password and upload again")
        sha = file_sha256(path)
        if c.execute("SELECT 1 FROM documents WHERE project_id=? AND sha256=?", (project_id, sha)).fetchone():
            _reject(c, user, project_id, path.name, "duplicate sha256 in project")
        if c.execute("SELECT 1 FROM documents WHERE project_id=? AND doc_id=?", (project_id, doc_id)).fetchone():
            _reject(c, user, project_id, path.name, f"doc_id {doc_id!r} already exists in project")
        try:
            parsed = parse_pdf(path, doc_id)
        except Exception as e:
            _reject(c, user, project_id, path.name, f"unreadable PDF: {e}")
        doc = {"doc_id": doc_id, "project_id": project_id, "title": title or parsed["metadata"].get("Document Title") or doc_id,
               "doc_type": doc_type, "version": version, "source_status": source_status, "sha256": sha, **parsed}
        out = processed_path(project_id, doc_id, processed_dir)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        c.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (doc_id, project_id, doc["title"], doc_type, version, source_status, sha,
                   json.dumps(doc["revisions"]), user, controls._now()))
        c.commit()
        n_chunks = 0
        if index:
            from app.chunk import chunk_document
            from app.retrieve import index_chunks
            chunks = chunk_document(doc)
            n_chunks = index_chunks(project_id, chunks, chroma_dir=chroma_dir)
        controls.log_action(user, "ingest", f"{project_id}:{doc_id}",
                            {"sections": len(doc["sections"]), "chunks": n_chunks, "sha256": sha}, c)
    return doc_id
