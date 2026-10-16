"""Build docs/TECHNICAL_REPORT.pdf from docs/TECHNICAL_REPORT.md and eval/results.json.

Usage: python scripts/build_report_pdf.py [--results eval/results.json] [--md docs/TECHNICAL_REPORT.md]
           [--out-md docs/TECHNICAL_REPORT.rendered.md] [--out-pdf docs/TECHNICAL_REPORT.pdf]

Steps: (1) resolve {{placeholders}} against the results JSON -> rendered markdown; (2) render ```mermaid blocks to PNG
with `mmdc` (mermaid-cli) when it is on PATH, otherwise leave the diagram source as a code block and print a note;
(3) write the PDF with reportlab (markdown subset: headings, paragraphs, bullets, tables, code blocks, images).

Placeholder syntax: ``{{path}}`` or ``{{path|format}}``.
  path    dot-separated keys, e.g. ``deterministic.diff_v1_v2.accuracy``. Keys may themselves contain dots or colons
          (``per_model.qwen2.5:3b.qa.summary.answer_correctness``): at each level the longest run of segments that
          forms an existing key is tried first, with backtracking. A bracket form is also accepted and is unambiguous:
          ``['per_model']['qwen2.5:3b']['qa']['summary']['answer_correctness']``. A numeric segment indexes a list.
  format  pct (0.753 -> 75%), pct1 (75.3%), f2 (0.25), f1, int, s (milliseconds -> "3.0 s"), len (length of a list),
          raw (default: str()).
A missing path, a null, or a value the format cannot handle renders as ``n/a (missing in results.json)``; never raises.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MISSING_TEXT = "n/a (missing in results.json)"
_MISSING = object()
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}|]+?)\s*(?:\|\s*(\w+)\s*)?\}\}")
_BRACKET = re.compile(r"\[\s*(?:'([^']*)'|\"([^\"]*)\"|([^\]\s]+))\s*\]")


# ------------------------------------------------------------------ placeholders
def split_path(path: str) -> list[str]:
    """Bracket form -> exact keys; dot form -> segments (joined greedily by the resolver)."""
    p = path.strip()
    if p.startswith("["):
        return [a or b or c for a, b, c in _BRACKET.findall(p)]
    return p.split(".")


def _walk(obj: Any, segs: list[str], exact: bool) -> Any:
    if not segs:
        return obj
    if isinstance(obj, dict):
        counts = [1] if exact else range(len(segs), 0, -1)
        for n in counts:
            key = ".".join(segs[:n])
            if key in obj:
                r = _walk(obj[key], segs[n:], exact)
                if r is not _MISSING:
                    return r
        return _MISSING
    if isinstance(obj, list):
        try:
            return _walk(obj[int(segs[0])], segs[1:], exact)
        except (ValueError, IndexError):
            return _MISSING
    return _MISSING


def lookup(data: Any, path: str) -> Any:
    """Value at ``path`` or the module sentinel _MISSING."""
    return _walk(data, split_path(path), path.strip().startswith("["))


def format_value(v: Any, fmt: str | None) -> str:
    """Format a number; raises ValueError/TypeError when the value does not fit (caller renders n/a)."""
    fmt = (fmt or "raw").lower()
    if fmt == "raw":
        return str(v)
    if fmt == "len":
        return str(len(v))  # list / dict / string length; TypeError for numbers
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError("not a number")
    if fmt == "pct":
        return f"{v * 100:.0f}%"
    if fmt == "pct1":
        return f"{v * 100:.1f}%"
    if fmt == "f2":
        return f"{v:.2f}"
    if fmt == "f1":
        return f"{v:.1f}"
    if fmt == "int":
        return str(int(round(v)))
    if fmt == "s":
        return f"{v / 1000:.1f} s"
    raise ValueError(f"unknown format {fmt!r}")


def resolve_placeholders(text: str, data: Any) -> tuple[str, list[str]]:
    """Replace every {{path|fmt}}; returns (text, list of unresolved placeholder paths)."""
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        path, fmt = m.group(1), m.group(2)
        v = lookup(data, path)
        if v is _MISSING or v is None:
            missing.append(path)
            return MISSING_TEXT
        try:
            return format_value(v, fmt)
        except (ValueError, TypeError):
            missing.append(f"{path}|{fmt}")
            return MISSING_TEXT

    return _PLACEHOLDER.sub(sub, text), missing


# ------------------------------------------------------------------ mermaid
_MERMAID = re.compile(r"```mermaid\s*\n(.*?)```", re.S)


def render_mermaid(text: str, out_dir: Path, mmdc: str | None = None) -> tuple[str, int, int]:
    """Replace mermaid blocks by PNG image links when mmdc works. Returns (text, n_rendered, n_blocks)."""
    blocks = _MERMAID.findall(text)
    if not blocks:
        return text, 0, 0
    exe = mmdc or shutil.which("mmdc")
    if not exe:
        return text, 0, len(blocks)
    out_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    counter = {"i": 0}

    def sub(m: re.Match) -> str:
        nonlocal done
        counter["i"] += 1
        src, png = out_dir / f"diagram_{counter['i']}.mmd", out_dir / f"diagram_{counter['i']}.png"
        src.write_text(m.group(1), encoding="utf-8")
        try:
            subprocess.run([exe, "-i", str(src), "-o", str(png), "-b", "white", "-s", "2"], check=True,
                           capture_output=True, timeout=120)
        except (subprocess.SubprocessError, OSError) as e:
            print(f"  mmdc failed on diagram {counter['i']}: {str(e)[:120]}; keeping source")
            return m.group(0)
        done += 1
        return f"![Diagram {counter['i']}]({png.as_posix()})\n"

    return _MERMAID.sub(sub, text), done, len(blocks)


# ------------------------------------------------------------------ PDF
_ASCII = {"→": "->", "←": "<-", "≥": ">=", "≤": "<=", "—": " - ", "–": "-", "‘": "'",
          "’": "'", "“": '"', "”": '"', "×": "x", "≈": "~", "•": "-", "¾": "-",
          "…": "...", " ": " ", "�": "?"}


def _clean(t: str) -> str:
    for k, v in _ASCII.items():
        t = t.replace(k, v)
    return t.encode("cp1252", "replace").decode("cp1252")


def _inline(t: str) -> str:
    """Escape XML and convert **bold**, *italic*, `code` to reportlab markup."""
    t = _clean(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r"`([^`]+)`", r'<font face="Courier" size="8.5">\1</font>', t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])\*([^*\s][^*]*)\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"\[([^\]]+)\]\((?!http)[^)]*\)", r"\1", t)
    return t


def parse_markdown(md: str) -> list[tuple]:
    """Markdown subset -> blocks: ('h',level,text) ('p',text) ('ul',[items]) ('table',[rows]) ('code',text) ('img',path,alt)."""
    lines = md.replace("\r\n", "\n").split("\n")
    blocks: list[tuple] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            blocks.append(("code", "\n".join(buf)))
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            blocks.append(("h", len(m.group(1)), m.group(2).strip()))
            i += 1
            continue
        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", ln.strip())
        if m:
            blocks.append(("img", m.group(2), m.group(1)))
            i += 1
            continue
        if ln.strip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            blocks.append(("table", rows))
            continue
        if re.match(r"^\s*([-*]|\d+[.)])\s+", ln):
            items = []
            while i < len(lines) and (re.match(r"^\s*([-*]|\d+[.)])\s+", lines[i]) or
                                      (lines[i].startswith("  ") and lines[i].strip() and items)):
                if re.match(r"^\s*([-*]|\d+[.)])\s+", lines[i]):
                    items.append(re.sub(r"^\s*([-*]|\d+[.)])\s+", "", lines[i]))
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            blocks.append(("ul", items))
            continue
        if not ln.strip():
            i += 1
            continue
        buf = [ln.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,6}\s|```|\||\s*([-*]|\d+[.)])\s+|!\[)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        blocks.append(("p", " ".join(buf)))
    return blocks


def write_pdf(md: str, out_pdf: Path, title: str = "Technical report", base_dir: Path | None = None) -> None:
    import textwrap

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Image, ListFlowable, ListItem, Paragraph, Preformatted, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=9.5, leading=12.5, spaceAfter=4)
    cell = ParagraphStyle("c", parent=body, fontSize=7.8, leading=9.5, spaceAfter=0)
    cellh = ParagraphStyle("ch", parent=cell, fontName="Helvetica-Bold")
    code = ParagraphStyle("code", fontName="Courier", fontSize=7.2, leading=8.6, backColor=colors.whitesmoke,
                          borderPadding=3, spaceAfter=6)
    heads = {1: ParagraphStyle("h1", parent=ss["Heading1"], fontSize=17, spaceBefore=10, spaceAfter=6),
             2: ParagraphStyle("h2", parent=ss["Heading2"], fontSize=13.5, spaceBefore=10, spaceAfter=4),
             3: ParagraphStyle("h3", parent=ss["Heading3"], fontSize=11, spaceBefore=8, spaceAfter=3)}
    width = A4[0] - 36 * mm
    story: list = []
    for b in parse_markdown(md):
        kind = b[0]
        if kind == "h":
            story.append(Paragraph(_inline(b[2]), heads.get(b[1], heads[3])))
        elif kind == "p":
            story.append(Paragraph(_inline(b[1]), body))
        elif kind == "ul":
            story.append(ListFlowable([ListItem(Paragraph(_inline(t), body), leftIndent=12) for t in b[1]],
                                      bulletType="bullet", start="-", leftIndent=12, bulletFontSize=8))
        elif kind == "code":
            wrapped = "\n".join(w for ln in _clean(b[1]).split("\n") for w in (textwrap.wrap(ln, 108) or [""]))
            story.append(Preformatted(wrapped, code))
        elif kind == "table" and b[1]:
            ncol = max(len(r) for r in b[1])
            rows = [[Paragraph(_inline(c), cellh if ri == 0 else cell) for c in r + [""] * (ncol - len(r))]
                    for ri, r in enumerate(b[1])]
            t = Table(rows, colWidths=[width / ncol] * ncol, repeatRows=1)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6e9ef")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
            story += [t, Spacer(1, 6)]
        elif kind == "img":
            p = Path(b[1])
            p = p if p.is_absolute() else (base_dir or ROOT) / p
            if p.is_file():
                from reportlab.lib.utils import ImageReader
                iw, ih = ImageReader(str(p)).getSize()
                s = min(width / iw, 200 * mm / ih, 1.0)
                story += [Image(str(p), width=iw * s, height=ih * s), Spacer(1, 6)]
            else:
                story.append(Paragraph(_inline(f"[image not found: {b[1]}]"), body))
    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"{title} - page {doc.page}")
        canvas.restoreState()

    SimpleDocTemplate(str(out_pdf), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                      bottomMargin=16 * mm, title=title).build(story, onFirstPage=footer, onLaterPages=footer)


# ------------------------------------------------------------------ main
def build(md_path: Path, results_path: Path, out_md: Path, out_pdf: Path) -> dict:
    data: Any = {}
    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"WARNING: cannot read {results_path} ({e}); every placeholder will render as n/a")
    text, missing = resolve_placeholders(md_path.read_text(encoding="utf-8"), data)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(text, encoding="utf-8")
    text2, n_ok, n_blocks = render_mermaid(text, out_pdf.parent / "_diagrams")
    if n_blocks and not n_ok:
        print(f"NOTE: {n_blocks} Mermaid diagram(s) were NOT rendered (mmdc not found on PATH or it failed). The PDF shows "
              "the diagram source as code. Install mermaid-cli (npm i -g @mermaid-js/mermaid-cli) and re-run, or paste "
              f"the blocks of {out_md.name} into https://mermaid.live and insert the PNGs.")
    elif n_blocks:
        print(f"Rendered {n_ok}/{n_blocks} Mermaid diagrams to PNG.")
    write_pdf(text2, out_pdf, base_dir=ROOT)
    return {"missing": missing, "mermaid_rendered": n_ok, "mermaid_blocks": n_blocks}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", default=str(ROOT / "eval" / "results.json"))
    ap.add_argument("--md", default=str(ROOT / "docs" / "TECHNICAL_REPORT.md"))
    ap.add_argument("--out-md", default=str(ROOT / "docs" / "TECHNICAL_REPORT.rendered.md"))
    ap.add_argument("--out-pdf", default=str(ROOT / "docs" / "TECHNICAL_REPORT.pdf"))
    a = ap.parse_args(argv)
    res = build(Path(a.md), Path(a.results), Path(a.out_md), Path(a.out_pdf))
    print(f"wrote {a.out_md} and {a.out_pdf}")
    if res["missing"]:
        print(f"WARNING: {len(res['missing'])} placeholder(s) unresolved (rendered as '{MISSING_TEXT}'):")
        for p in sorted(set(res["missing"])):
            print("  -", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
