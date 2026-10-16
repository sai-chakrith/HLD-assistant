"""Inspect the real AUTOSAR PDFs (PyMuPDF + pdfplumber) and write docs/data_notes.md (auto-generated part)."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf as fitz  # PyMuPDF
import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402

MARKER = "## Parser design implications"
NUM_HEAD = re.compile(r"^(\d+(?:\.\d+){0,5})\s+(\S.*)$")
KEYWORDS = {"swc": r"software component|\bSWC\b", "port": r"\bports?\b", "interface": r"interface",
            "signal": r"signal", "data element": r"data ?element"}


def font_stats(doc: fitz.Document, max_pages: int = 400) -> Counter:
    c: Counter = Counter()
    for page in doc.pages(0, min(doc.page_count, max_pages)):
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    c[(round(sp["size"], 1), "bold" if sp["flags"] & 16 or "Bold" in sp["font"] else "reg")] += len(sp["text"])
    return c


def headings(doc: fitz.Document) -> list[dict]:
    """Numbered headings (page>=5). Handles 'N Title' on one line and number/title on separate lines (HMI)."""
    out = []
    for pno, page in enumerate(doc, 1):
        if pno < 5:  # skip cover, disclaimer, table of contents
            continue
        lines = []
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                text = "".join(s["text"] for s in ln["spans"]).strip()
                if text and ln["spans"]:
                    lines.append((text, ln["spans"][0]))
        for i, (text, sp) in enumerate(lines):
            if re.match(r"^\d+ of \d+$", text) or "...." in text:
                continue
            m = NUM_HEAD.match(text)
            num, title = (m.group(1), m.group(2)) if m else (None, None)
            if not m and re.fullmatch(r"\d+(?:\.\d+){0,5}", text) and sp["size"] >= 12 and i + 1 < len(lines):
                num, title = text, lines[i + 1][0]
            if num:
                out.append({"page": pno, "num": num, "title": title[:90], "size": round(sp["size"], 1),
                            "bold": bool(sp["flags"] & 16 or "Bold" in sp["font"]), "font": sp["font"]})
    return out


def repeated_lines(doc: fitz.Document) -> list[tuple[str, int, str]]:
    """Lines in top/bottom 9% of the page repeated on many pages (digits normalised)."""
    cnt: Counter = Counter()
    where: dict[str, str] = {}
    n = doc.page_count
    for page in doc:
        h = page.rect.height
        for x0, y0, x1, y1, text, *_ in page.get_text("blocks"):
            zone = "header" if y1 < h * 0.09 else "footer" if y0 > h * 0.90 else None
            if zone:
                for line in text.splitlines():
                    norm = re.sub(r"\d+", "#", line.strip())
                    if norm:
                        cnt[norm] += 1
                        where[norm] = f"{zone} y0={y0:.0f}"
    return [(k, v, where[k]) for k, v in cnt.most_common(12) if v >= max(3, n * 0.3)]


def tables_info(path: Path) -> list[dict]:
    res = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            for t in page.find_tables():
                rows = t.extract()
                if rows:
                    res.append({"page": pno, "rows": len(rows), "cols": len(rows[0]), "bbox": [round(v) for v in t.bbox],
                                "header": [(c or "").replace("\n", " ").strip() for c in rows[0]],
                                "page_h": round(page.height)})
    return res


def continued(tabs: list[dict]) -> list[tuple[int, int, list[str]]]:
    out = []
    for a, b in zip(tabs, tabs[1:]):
        if b["page"] == a["page"] + 1 and a["header"] == b["header"]:
            out.append((a["page"], b["page"], a["header"]))
    return out


def section_keywords(doc: fitz.Document, heads: list[dict]) -> dict[str, list[str]]:
    """Sections (level<=3) whose text mentions each keyword most often."""
    texts = [p.get_text() for p in doc]
    hs = [h for h in heads if h["page"] >= 5]
    res: dict[str, Counter] = defaultdict(Counter)
    for i, h in enumerate(hs):
        end = hs[i + 1]["page"] if i + 1 < len(hs) else doc.page_count
        body = " ".join(texts[h["page"] - 1:end])
        for k, rx in KEYWORDS.items():
            res[k][f"{h['num']} {h['title']} (p{h['page']})"] = len(re.findall(rx, body, re.I))
    return {k: [f"{n}: {c}" for n, c in v.most_common(5)] for k, v in res.items()}


def change_history(doc: fitz.Document, tabs: list[dict]) -> list[str]:
    lines = []
    for pno, page in enumerate(doc, 1):
        t = page.get_text()
        if re.search(r"Document Change History|Change History", t, re.I):
            lines.append(f"p{pno}: " + " | ".join(l for l in t.splitlines() if re.search(r"Change History", l, re.I))[:150])
    ch = [t for t in tabs if any(re.search(r"date|release|changed by", h, re.I) for h in t["header"])]
    lines += [f"table p{t['page']} header={t['header']} rows={t['rows']}" for t in ch[:6]]
    return lines


def inspect(entry: dict) -> str:
    path = settings.raw_dir / entry["filename"]
    doc = fitz.open(path)
    md = [f"## {entry['doc_id']} ({entry['pages']} pages, {entry['release']})", ""]
    fs = font_stats(doc)
    md += ["### Font sizes (chars, top 8)", ""] + [f"- {s}pt {w}: {n}" for (s, w), n in fs.most_common(8)] + [""]
    hs = headings(doc)
    hsz = Counter((h["size"], h["bold"]) for h in hs if h["page"] >= 5)
    md += ["### Numbered headings: size/bold -> count (page>=5)", ""] + [f"- {s}pt bold={b}: {n}" for (s, b), n in hsz.most_common()] + [""]
    by_depth: dict[int, Counter] = defaultdict(Counter)
    for h in hs:
        if h["page"] >= 5:
            by_depth[h["num"].count(".") + 1][h["size"]] += 1
    md += ["Heading depth -> sizes: " + "; ".join(f"L{d}: {dict(c)}" for d, c in sorted(by_depth.items())), ""]
    md += ["First 25 numbered headings after page 4:", ""]
    md += [f"- p{h['page']} `{h['num']}` {h['title']} ({h['size']}pt, {h['font']})" for h in [h for h in hs if h["page"] >= 5][:25]] + [""]
    md += ["### Repeated header/footer lines (digits->#)", ""]
    md += [f"- `{k}` x{v} ({w})" for k, v, w in repeated_lines(doc)] + [""]
    tabs = tables_info(path)
    per_page = Counter(t["page"] for t in tabs)
    md += [f"### Tables: {len(tabs)} total on {len(per_page)} pages", ""]
    hdrs = Counter(tuple(t["header"]) for t in tabs)
    md += ["Distinct header rows (count):", ""] + [f"- {n}x `{list(h)}`" for h, n in hdrs.most_common(25)] + [""]
    cont = continued(tabs)
    md += [f"Tables continued across consecutive pages with identical first row: {len(cont)}", ""]
    md += [f"- pages {a}->{b} header {h}" for a, b, h in cont[:10]] + [""]
    md += ["### Change history", ""] + [f"- {l}" for l in change_history(doc, tabs)] + [""]
    md += ["### Sections most mentioning keywords", ""]
    for k, v in section_keywords(doc, hs).items():
        md += [f"- **{k}**: " + "; ".join(v)]
    md += [""]
    return "\n".join(md)


def main() -> None:
    manifest = json.loads((settings.raw_dir / "manifest.json").read_text(encoding="utf-8"))
    parts = ["# Data notes (real AUTOSAR PDFs)", "",
             "Auto-generated by scripts/inspect_pdfs.py. See the hand-written section at the end.", ""]
    for e in manifest:
        print("inspecting", e["doc_id"])
        parts.append(inspect(e))
    out = settings.project_root / "docs" / "data_notes.md"
    tail = ""
    if out.exists():  # preserve hand-written sections added after the generated part
        old = out.read_text(encoding="utf-8")
        if MARKER in old:
            tail = "\n" + old[old.index(MARKER):]
    out.write_text("\n".join(parts) + tail, encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    main()
