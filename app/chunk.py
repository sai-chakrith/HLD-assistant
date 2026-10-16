"""Section-aware chunking: text chunks (~target tokens, small overlap) and table chunks (markdown)."""
from __future__ import annotations

import math
import re
from typing import Any

from app.config import settings


def est_tokens(text: str) -> int:
    """Cheap token estimate (~1.3 tokens per word)."""
    return math.ceil(len(text.split()) * 1.3)


def _units(text: str) -> list[str]:
    """Split text into sentence-ish units, paragraph by paragraph."""
    units: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9•])", para)
        units.extend(p for p in parts if p)
    return units


def _split_long(unit: str, max_tokens: int) -> list[str]:
    words = unit.split()
    step = max(1, int(max_tokens / 1.3))
    return [" ".join(words[i:i + step]) for i in range(0, len(words), step)]


def split_text(text: str, target: int, max_tokens: int, overlap: int) -> list[str]:
    """Pack units into chunks of ~target tokens (never above max), carrying `overlap` tokens forward."""
    units: list[str] = []
    for u in _units(text):
        units.extend(_split_long(u, max_tokens) if est_tokens(u) > max_tokens else [u])
    chunks: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for u in units:
        t = est_tokens(u)
        if cur and cur_tok + t > target:
            chunks.append("\n".join(cur))
            keep: list[str] = []
            kt = 0
            for prev in reversed(cur):
                if kt + est_tokens(prev) > overlap:
                    break
                keep.insert(0, prev)
                kt += est_tokens(prev)
            cur, cur_tok = keep, kt
        cur.append(u)
        cur_tok += t
    if cur:
        tail = "\n".join(cur)
        # avoid a chunk that is only the carried-over overlap
        if not chunks or any(est_tokens(u) for u in cur if u not in chunks[-1]):
            chunks.append(tail)
    return chunks


def _md_cell(c: str) -> str:
    return c.replace("|", "\\|")


def table_to_markdown(header: list[str], rows: list[list[str]]) -> str:
    width = max([len(header)] + [len(r) for r in rows] or [1])
    head = header or [""] * width
    pad = lambda r: [_md_cell(x) for x in r] + [""] * (width - len(r))  # noqa: E731
    lines = ["| " + " | ".join(pad(head)) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(pad(r)) + " |" for r in rows]
    return "\n".join(lines)


def _table_chunks(prefix: str, header: list[str], rows: list[list[str]], max_tokens: int) -> list[str]:
    out: list[str] = []
    batch: list[list[str]] = []
    for r in rows:
        trial = prefix + table_to_markdown(header, batch + [r])
        if batch and est_tokens(trial) > max_tokens:
            out.append(prefix + table_to_markdown(header, batch))
            batch = []
        batch.append(r)
    if batch or not out:
        out.append(prefix + table_to_markdown(header, batch))
    return out


def _chunk_type(text: str) -> str:
    paras = [p for p in text.split("\n") if p.strip()]
    if len(paras) >= 6 and sum(len(p.split()) for p in paras) / len(paras) < 4:
        return "figure_text"  # mostly short diagram labels
    return "text"


def chunk_document(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Return [{id, text, metadata}] for a processed document. A chunk never spans two sections."""
    chunks: list[dict[str, Any]] = []
    target, mx, ov = settings.chunk_target_tokens, settings.chunk_max_tokens, settings.chunk_overlap_tokens
    for sec in doc["sections"]:
        sid, stitle = sec["section_id"], sec["title"]
        prefix = f"{sid} {stitle}\n\n" if sid != "0" else f"{stitle}\n\n"
        budget = mx - est_tokens(prefix)
        pieces: list[tuple[str, str, int, int]] = []
        is_ocr = bool(sec.get("ocr"))
        oconf = float(sec.get("ocr_confidence") or -1) if is_ocr else -1.0
        if sec["text"].strip():
            ctype = _chunk_type(sec["text"])
            for t in split_text(sec["text"], target - est_tokens(prefix), budget, ov):
                pieces.append((prefix + t, ctype, sec["page_start"], sec["page_end"]))
        for tab in sec["tables"]:
            for t in _table_chunks(prefix, tab["header"], tab["rows"], mx):
                pieces.append((t, "table", tab["page_start"], tab["page_end"]))
        for n, (text, ctype, p0, p1) in enumerate(pieces):
            chunks.append({
                "id": f"{doc['doc_id']}::{sid}::{n}",
                "text": text,
                "metadata": {
                    "project_id": doc["project_id"], "doc_id": doc["doc_id"], "doc_type": doc["doc_type"],
                    "version": doc["version"], "page_start": int(p0), "page_end": int(p1),
                    "section_id": sid, "section_title": stitle, "chunk_type": ctype,
                    "ocr": is_ocr, "ocr_confidence": oconf,
                },
            })
    return chunks
