"""Diagram geometry extraction (method="diagram").

Real AUTOSAR HLD diagrams are vector drawings: components are (dashed) boxes, ports are text labels sitting
just inside/outside a box border, signals are `Name / kind` labels printed on arrow lines between boxes.

Pipeline per page (all thresholds in app.config.settings, diag_*):
 1. flatten page.get_drawings() into boxes, arrow-head triangles, port markers (tiny squares) and line segments
 2. chain segments into connected paths; a path with an arrow head is an arrow (tail endpoints -> head endpoint)
 3. text lines (page.get_text("dict")): title lines of a box, port labels (near a border), signal labels
 4. assign: title -> smallest box containing it; port label -> box with the nearest border; signal label ->
    nearest arrow segment; arrow ends -> box whose border they touch.

Public API: analyze_pdf(path) -> list[PageDiagram]; extract_diagram_entities(doc, pdf_path) -> {"components": [...], ...};
find_pdf(doc); render_debug(pdf_path, page_no, out_png, ...).
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings

SIGNAL_LABEL = re.compile(
    r"^(?P<name>[A-Z][A-Za-z0-9_]{2,})\s*/\s*(?P<elem>command|request|status|display|value|failure)\s*$", re.I)
PREFIXED = re.compile(r"^(Cmd|Req|Sta|Dis|Err|Hmi)[A-Z]")
_INSTANCE = re.compile(r"\[[^\]]*\]")
_NOT_TITLE = re.compile(r"^(?:\w+\s+)?SW-?C$|^(?-i:(actuator|sensor|adaptation)(\s+SW-?)?)$|^C$|^not used by|not further|broken down|"
                        r"^\(?non-?atomic|^fig\b|^\d+$", re.I)


# ---------------------------------------------------------------- data classes

@dataclass
class Box:
    idx: int
    rect: tuple  # x0, y0, y1.. stored as (x0, y0, x1, y1)
    order: int = 0
    title_lines: list = field(default_factory=list)
    title: str = ""
    category: str = ""
    ports: list = field(default_factory=list)
    alias: "Box | None" = None

    @property
    def area(self) -> float:
        return max(0.0, (self.rect[2] - self.rect[0]) * (self.rect[3] - self.rect[1]))


@dataclass
class Line:
    text: str
    bbox: tuple
    rotated: bool = False
    dir: tuple = (1, 0)
    bold: bool = False
    italic: bool = False
    size: float = 0.0
    ocr: bool = False  # read by OCR from an embedded label image (see _tile_lines)


@dataclass
class PortLabel:
    name: str
    line: Line
    box: Box
    side: str
    gap: float
    inside: bool
    direction: str = "unknown"
    signals: list = field(default_factory=list)


@dataclass
class SignalLabel:
    name: str
    elem: str
    line: Line
    arrow: int | None = None
    arrow_dist: float = 99.0
    sender: Box | None = None
    sender_dist: float = 99.0
    receivers: list = field(default_factory=list)  # (Box, dist)


@dataclass
class PageDiagram:
    page: int
    boxes: list
    ports: list
    signals: list
    arrows: list
    markers: list
    segments: list
    heads: list
    height: float = 842.0


# ---------------------------------------------------------------- helpers

def _fix_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    if len(t) >= 4 and len(t) % 2 == 0 and t[0::2] == t[1::2]:  # 'FFeeaattuurree' -> 'Feature'
        t = t[0::2]
    return t


def _inter(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def _iou(a, b) -> float:
    i = _inter(a, b)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


def _pt_seg_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _border_dist(pt, rect) -> float:
    """Distance from point to the rectangle OUTLINE (0 when on the border)."""
    x, y = pt
    x0, y0, x1, y1 = rect
    inside = x0 <= x <= x1 and y0 <= y <= y1
    if inside:
        return min(x - x0, x1 - x, y - y0, y1 - y)
    dx = max(x0 - x, 0, x - x1)
    dy = max(y0 - y, 0, y - y1)
    return math.hypot(dx, dy)


# ---------------------------------------------------------------- drawings -> primitives

def _collect_primitives(page):
    import pymupdf
    s = settings
    boxes: list[tuple] = []
    heads: list[tuple] = []     # triangle centroids
    markers: list[tuple] = []   # small squares (port markers)
    segs: list[tuple] = []      # (x0,y0,x1,y1)
    for dr in page.get_drawings():
        items = [(i[0], pymupdf.Rect(i[1].rect) if i[0] == "qu" else i[1], *i[2:]) if i[0] in ("re", "qu") else i
                 for i in dr["items"]]
        typ = dr.get("type", "")
        r = dr["rect"]
        w, h = r.width, r.height
        stroked = "s" in typ
        bigrect = any(i[0] in ("re", "qu") and i[1].width >= s.diag_min_box and i[1].height >= s.diag_min_box
                      for i in items)
        dashed_box = len(items) >= 8 and w >= s.diag_min_box and h >= s.diag_min_box and stroked
        if (bigrect and stroked) or dashed_box:
            if bigrect and stroked and len(items) <= 4:
                for i in items:
                    if i[0] in ("re", "qu") and i[1].width >= s.diag_min_box and i[1].height >= s.diag_min_box:
                        boxes.append((i[1].x0, i[1].y0, i[1].x1, i[1].y1))
            else:
                boxes.append((r.x0, r.y0, r.x1, r.y1))
            continue
        if "f" == typ.strip() and not stroked:
            # pure fill: arrow head triangle(s) or white backdrops; backdrops are skipped
            pass
        ls = [i for i in items if i[0] == "l"]
        # closed triangle among consecutive 'l' items
        used = set()
        for k in range(len(ls) - 2):
            a, b, c = ls[k], ls[k + 1], ls[k + 2]
            pts = [a[1], a[2], b[2]]
            if (math.hypot(c[2].x - a[1].x, c[2].y - a[1].y) < 0.6 and abs(b[1].x - a[2].x) < 0.6
                    and abs(c[1].x - b[2].x) < 0.6):
                xs = [p.x for p in pts]
                ys = [p.y for p in pts]
                if max(xs) - min(xs) <= 11 and max(ys) - min(ys) <= 11:
                    heads.append(tuple(zip(xs, ys)))
                    used.update((id(a), id(b), id(c)))
        for i in items:
            if i[0] == "l" and id(i) not in used:
                p1, p2 = i[1], i[2]
                if math.hypot(p2.x - p1.x, p2.y - p1.y) > 0.5:
                    segs.append((p1.x, p1.y, p2.x, p2.y))
            elif i[0] in ("re", "qu"):
                rr = i[1]
                if 3 <= rr.width <= 9 and 3 <= rr.height <= 9 and stroked and not dr.get("fill") == (0.0, 0.0, 0.0):
                    markers.append((rr.x0, rr.y0, rr.x1, rr.y1))
                elif min(rr.width, rr.height) <= 2.2 and max(rr.width, rr.height) > 3:
                    if rr.width >= rr.height:
                        yy = (rr.y0 + rr.y1) / 2
                        segs.append((rr.x0, yy, rr.x1, yy))
                    else:
                        xx = (rr.x0 + rr.x1) / 2
                        segs.append((xx, rr.y0, xx, rr.y1))
    uniq: dict[tuple, tuple] = {}
    for sg in segs:  # overprinted / reversed duplicates would otherwise hide a line's free end
        a, b = (round(sg[0], 1), round(sg[1], 1)), (round(sg[2], 1), round(sg[3], 1))
        uniq.setdefault((min(a, b), max(a, b)), sg)
    return boxes, heads, markers, list(uniq.values())


def _port_glyphs(page) -> list[tuple[float, float, float]]:
    """Horizontal open chevrons / closed arrow glyphs, with their actual pointing direction.

    These occur beside AUTOSAR port squares even when the connecting line has no filled triangle.
    Text labels and port names are deliberately not used to infer direction.
    """
    glyphs = []
    for drawing in page.get_drawings():
        rect = drawing["rect"]
        lines = [item for item in drawing["items"] if item[0] == "l"]
        if not (2 <= len(lines) <= 5 and 2 <= rect.width <= 11 and 3 <= rect.height <= 12):
            continue
        for first, second in zip(lines, lines[1:]):
            a, tip, joined, b = first[1], first[2], second[1], second[2]
            if math.hypot(tip.x - joined.x, tip.y - joined.y) > 0.5:
                continue
            if (abs(a.x - b.x) <= 0.5 and abs(tip.y - (a.y + b.y) / 2) <= 0.6
                    and 1.5 <= abs(tip.x - a.x) <= 8 and 3 <= abs(a.y - b.y) <= 12):
                glyphs.append((tip.x, tip.y, tip.x - a.x))
    return glyphs


def _glyph_direction(port: PortLabel, glyphs: list[tuple]) -> str:
    center = _center(port.line.bbox)
    edge = port.box.rect[0] if port.side == "left" else port.box.rect[2]
    candidates = []
    for x, y, dx in glyphs:
        if abs(y - center[1]) > settings.diag_port_arrow_y or abs(x - edge) > settings.diag_touch_tol + 9:
            continue
        # A glyph must be outside or on this border, not an unrelated arrow inside the component.
        if (port.side == "left" and x > edge + 1) or (port.side == "right" and x < edge - 1):
            continue
        inward = dx > 0 if port.side == "left" else dx < 0
        candidates.append((abs(y - center[1]) + abs(x - edge) / 10, "required" if inward else "provided"))
    candidates.sort()
    if not candidates or (len(candidates) > 1 and candidates[0][1] != candidates[1][1]
                          and candidates[1][0] - candidates[0][0] < 0.5):
        return "unknown"
    return candidates[0][1]


def _dedupe_boxes(raw: list[tuple]) -> list[Box]:
    out: list[Box] = []
    for k, r in enumerate(raw):
        dup = False
        for b in out:
            if _iou(b.rect, r) > 0.9:
                dup = True
                break
        if not dup:
            out.append(Box(idx=len(out), rect=r, order=k))
    return out


# ---------------------------------------------------------------- segments -> arrows

def _chain_segments(segs: list[tuple], tol: float) -> list[list[int]]:
    n = len(segs)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ends = [((s[0], s[1]), (s[2], s[3])) for s in segs]
    for i in range(n):
        for j in range(i + 1, n):
            for p in ends[i]:
                if _pt_seg_dist(p, ends[j][0], ends[j][1]) <= tol:
                    parent[find(i)] = find(j)
                    break
            else:
                for q in ends[j]:
                    if _pt_seg_dist(q, ends[i][0], ends[i][1]) <= tol:
                        parent[find(i)] = find(j)
                        break
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _build_arrows(segs, heads, tol):
    """Return arrows: dict(seg_ids, tails: [pt], heads: [pt])."""
    arrows = []
    for grp in _chain_segments(segs, tol):
        pts = []  # free endpoints: endpoints not lying on another segment of the group
        for i in grp:
            for p in ((segs[i][0], segs[i][1]), (segs[i][2], segs[i][3])):
                touched = any(j != i and _pt_seg_dist(p, (segs[j][0], segs[j][1]), (segs[j][2], segs[j][3])) <= tol
                              for j in grp)
                if not touched:
                    pts.append(p)
        hd = []
        tipped: list[tuple] = []
        for tri in heads:
            cx = sum(v[0] for v in tri) / 3
            cy = sum(v[1] for v in tri) / 3
            best = min(pts, key=lambda p: math.hypot(p[0] - cx, p[1] - cy), default=None)
            if best and math.hypot(best[0] - cx, best[1] - cy) <= 9:
                # the apex of the triangle (farthest vertex from the line end) is where the arrow lands
                tip = max(tri, key=lambda v: math.hypot(v[0] - best[0], v[1] - best[1]))
                if tip not in hd:
                    hd.append(tip)
                    tipped.append(best)
        pts = [p for p in pts if p not in tipped]
        if not hd:
            continue
        tails = list(pts)
        arrows.append({"segs": grp, "tails": tails, "heads": hd})
    return arrows


# ---------------------------------------------------------------- text

def _text_lines(page) -> list[Line]:
    out: list[Line] = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            spans = [s for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            t = _fix_text("".join(s["text"] for s in l["spans"]))
            if not t:
                continue
            d = tuple(round(x) for x in l["dir"])
            bb = tuple(l["bbox"])
            if any(o.text == t and abs(o.bbox[0] - bb[0]) < 2 and abs(o.bbox[1] - bb[1]) < 2 for o in out):
                continue  # text drawn twice (overprint) -> keep one
            out.append(Line(text=t, bbox=bb, rotated=d != (1, 0), dir=d,
                            bold=bool(spans[0]["flags"] & 16) or "bold" in spans[0]["font"].lower(),
                            italic=bool(spans[0]["flags"] & 2) or "italic" in spans[0]["font"].lower(),
                            size=spans[0]["size"]))
    return out


# ---------------------------------------------------------------- labels stored as image tiles
# Some diagrams keep the boxes and arrows as vector drawings but print every label as small, very high resolution
# image tiles (no text layer). Those tiles are grouped into labels and read by OCR one label at a time; a label is
# kept only when every identifier in it also occurs in the document's text layer (``vocab``), so OCR misreads
# ('Reg...' for 'Req...') are dropped instead of guessed. Large single raster figures are not read this way.
TILE_MAX_H = 30.0       # pt: one text line (kind tiles carry blank padding, up to ~26 pt)
TILE_MIN_SCALE = 4.0    # px per pt (~290 dpi or more): rendered text, not a photo or a figure
TILE_MIN_COUNT = 10     # tiles needed before a page is treated as having image labels
_TILE_DPI = 500
_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_LABEL_TEXT = re.compile(r"^\[?[A-Za-z][A-Za-z0-9_\]\[ .:/\-()]*$")


def doc_vocab(pdf) -> set[str]:
    """Identifier-like words (>= 3 chars) of a PDF's text layer: the reference that OCR labels must match."""
    words: set[str] = set()
    for page in pdf:
        words.update(w for w in _IDENT.findall(page.get_text()) if len(w) >= 3)
    return words


_KINDS = ("command", "request", "status", "display", "value", "failure")


def _verified(token: str, vocab: set[str]) -> bool:
    """A label word is verified when the text layer has it, or (>= 4 chars) has it inside a longer identifier."""
    if token in vocab:
        return True
    return len(token) >= 4 and any(token in v for v in vocab if len(v) > len(token))


def _verified_label(text: str, vocab: set[str]) -> str | None:
    """The label text when all its identifiers are verified (also tried with OCR's stray inner spaces removed:
    'Mi rrorAdjust' -> 'MirrorAdjust'); signal kinds and instance brackets are not checked; None otherwise."""
    m = re.match(r"^(?P<name>.+?)\s*/\s*(?P<kind>\w+)$", text)
    head, tail = (m.group("name"), f" / {m.group('kind').lower()}") if m and m.group("kind").lower() in _KINDS \
        else (text, "")
    for cand in (head, re.sub(r"(?<=[A-Za-z]) (?=[a-z])", "", head)):
        idents = [t for t in _IDENT.findall(_INSTANCE.sub(" ", cand)) if t.lower() not in _KINDS]
        if idents and all(len(t) >= 3 and _verified(t, vocab) for t in idents):
            return cand + tail
    return None


def _tile_groups(page) -> list[tuple]:
    """Label rectangles built from adjacent high-resolution image tiles on one text line."""
    import pymupdf
    tiles = []
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"])
        if 0 < r.height <= TILE_MAX_H and r.width > 0 and info["width"] / r.width >= TILE_MIN_SCALE:
            tiles.append(r)
    if len(tiles) < TILE_MIN_COUNT:
        return []
    tiles.sort(key=lambda r: (round(r.y0), r.x0))
    groups: list[list] = []
    for r in tiles:
        for g in groups:
            ov = min(g[3], r.y1) - max(g[1], r.y0)
            if ov >= 0.6 * min(g[3] - g[1], r.height) and -1.0 <= r.x0 - g[2] <= 2.5:
                g[0], g[1], g[2], g[3] = min(g[0], r.x0), min(g[1], r.y0), max(g[2], r.x1), max(g[3], r.y1)
                break
        else:
            groups.append([r.x0, r.y0, r.x1, r.y1])
    # thin slivers (the '/' of 'Name / kind', brackets, borders) carry no readable word on their own
    return [tuple(g) for g in groups if g[2] - g[0] >= 6 and g[3] - g[1] >= 4]


def _tile_lines(page, vocab: set[str] | None) -> list[Line]:
    """OCR each tile label (single text line) and keep the verified ones as Line objects (ocr=True)."""
    if not vocab:
        return []
    groups = _tile_groups(page)
    if not groups:
        return []
    from app import ocr
    if not ocr.tesseract_available():
        return []
    import pymupdf
    import pytesseract
    from PIL import Image
    raw: list[list] = []  # [text, bbox]
    for g in groups:
        clip = pymupdf.Rect(g[0] - 1.5, g[1] - 1.5, g[2] + 1.5, g[3] + 1.5)
        pix = page.get_pixmap(dpi=_TILE_DPI, clip=clip)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
        try:
            d = pytesseract.image_to_data(img, config="--psm 7", output_type=pytesseract.Output.DICT)
        except Exception:  # noqa: BLE001 - a label that cannot be read is skipped
            continue
        idx = [i for i, (w, c) in enumerate(zip(d["text"], d["conf"])) if w.strip() and float(c) >= 0]
        text = _fix_text(" ".join(d["text"][i].strip() for i in idx))
        if text:  # font size from the glyph height OCR measured (tile heights vary), ~0.72 of the em size
            size = round(min(max(d["height"][i] for i in idx) * 72.0 / _TILE_DPI / 0.72, (g[3] - g[1]) * 0.95), 1)
            raw.append([text, list(g), size])
    # 'Name' + '/' + 'kind' printed as separate tiles on one baseline -> one signal label
    raw.sort(key=lambda x: (round(x[1][1]), x[1][0]))
    merged: list[list] = []
    for text, bb, size in raw:
        prev = merged[-1] if merged else None
        same_line = prev is not None and (min(prev[1][3], bb[3]) - max(prev[1][1], bb[1])
                                          >= 0.5 * min(bb[3] - bb[1], prev[1][3] - prev[1][1]))
        joins = text.startswith("/") or prev is not None and prev[0].endswith("/") or text.lower() in _KINDS
        if same_line and joins and 0 <= bb[0] - prev[1][2] <= 8:
            sep = " " if "/" in prev[0] + text else " / "  # the '/' tile itself is too thin to be read
            prev[0] = re.sub(r"\s*/\s*", " / ", f"{prev[0]}{sep}{text}").strip()
            prev[1] = [prev[1][0], prev[1][1], max(prev[1][2], bb[2]), prev[1][3]]  # keep the name's line height
        else:
            merged.append([text, bb, size])
    # a label wrapped onto two lines ('VehicleLongitudinal' over 'Speed') is one label when the joined word is known
    for i, (text, bb, size) in enumerate(merged):
        for nxt in merged[i + 1:]:
            ntext, nbb, _ = nxt
            below = -3 <= nbb[1] - bb[3] <= 0.6 * (bb[3] - bb[1]) + 2 and nbb[1] > bb[1] + 2
            overlap = min(bb[2], nbb[2]) - max(bb[0], nbb[0]) > 0
            if below and overlap and "/" not in text + ntext and text + ntext in vocab:
                merged[i] = [text + ntext, [min(bb[0], nbb[0]), bb[1], max(bb[2], nbb[2]), nbb[3]], size]
                nxt[0] = ""
                break
    out: list[Line] = []
    for text, bb, size in merged:
        if not text or not _LABEL_TEXT.match(text):
            continue
        label = _verified_label(text, vocab)
        if label:
            out.append(Line(text=label, bbox=tuple(bb), size=size, ocr=True))
    return out


def _center(bb):
    return ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)


def _inside(pt, rect, pad=0.0) -> bool:
    return rect[0] - pad <= pt[0] <= rect[2] + pad and rect[1] - pad <= pt[1] <= rect[3] + pad


def _smallest_box(boxes, pt, pad=0.0):
    c = [b for b in boxes if _inside(pt, b.rect, pad)]
    if not c:
        return None
    m = min(b.area for b in c)
    near = [b for b in c if b.area <= m * 1.15]
    return max(near, key=lambda b: b.order)  # front-most among near-equal (stacked instances)


# ---------------------------------------------------------------- page analysis

def analyze_page(page, page_no: int, vocab: set[str] | None = None) -> PageDiagram:
    """``vocab`` (``doc_vocab``) enables reading labels stored as image tiles; without it only the text layer is used."""
    s = settings
    raw_boxes, heads, markers, segs = _collect_primitives(page)
    boxes = _dedupe_boxes(raw_boxes)
    arrows = _build_arrows(segs, heads, s.diag_join_tol)
    lines = _text_lines(page)
    if boxes and vocab:
        lines += _tile_lines(page, vocab)
    sig_lines: list[Line] = []
    other: list[Line] = []
    for ln in lines:
        m = SIGNAL_LABEL.match(ln.text)
        (sig_lines if m else other).append(ln)

    # ---- classify non-signal text lines against boxes
    sizes = sorted(round(x.size, 1) for x in sig_lines)
    ref_size = sizes[len(sizes) // 2] if sizes else None
    ports: list[PortLabel] = []
    title_lines: dict[int, list[Line]] = {}
    for ln in other:
        c = _center(ln.bbox)
        box = _smallest_box(boxes, c)
        if box is not None:
            r = box.rect
            gl, gr = ln.bbox[0] - r[0], r[2] - ln.bbox[2]
            if ln.rotated:
                if re.search(r"[A-Za-z]{2}", ln.text):
                    title_lines.setdefault(box.idx, []).append(ln)
                continue
            if _NOT_TITLE.search(ln.text) or (ln.italic and not ln.bold):
                m = re.match(r"^(actuator|sensor|adaptation)\b", ln.text, re.I)
                if m:
                    box.category = m.group(1).lower()
                continue
            near = min(gl, gr)
            asym = abs(gl - gr)
            is_title = ln.bold or (ref_size is not None and ln.size > ref_size + 0.7)
            has_alpha = re.search(r"[A-Za-z]{3}", ln.text)
            port_like = (not is_title and near <= s.diag_port_inside_margin and
                         (ref_size is not None or asym > s.diag_port_asym))
            if port_like and has_alpha:
                side = "left" if gl < gr else "right"
                ports.append(PortLabel(_INSTANCE.sub("", ln.text).strip(), ln, box, side, max(near, 0.0), True))
            elif has_alpha and (is_title or not ln.ocr):  # OCR lines carry no bold flag: only size makes a title
                title_lines.setdefault(box.idx, []).append(ln)
        else:
            # outside every box: candidate external port label next to a border that has a marker / arrow end
            if ln.rotated or _NOT_TITLE.search(ln.text) or not re.search(r"[A-Za-z]{3}", ln.text):
                continue
            best = None
            for b in boxes:
                r = b.rect
                if not (r[1] - 2 <= c[1] <= r[3] + 2):
                    continue
                for side, edge, gap in (("left", r[0], r[0] - ln.bbox[2]), ("right", r[2], ln.bbox[0] - r[2])):
                    if -1.0 <= gap <= s.diag_port_outside_dist:
                        if best is None or gap < best[0]:
                            best = (gap, b, side, edge)
            if best is None:
                continue
            gap, b, side, edge = best
            has_support = any(abs((m[1] + m[3]) / 2 - c[1]) <= 5 and abs((m[0] + m[2]) / 2 - edge) <= 6
                              for m in markers)
            if not has_support:
                for a in arrows:
                    for p in a["tails"] + a["heads"]:
                        if abs(p[1] - c[1]) <= 5 and abs(p[0] - edge) <= 6:
                            has_support = True
            if has_support:
                ports.append(PortLabel(_INSTANCE.sub("", ln.text).strip(), ln, b, side, max(gap, 0.0), False))

    # ---- box titles
    for bidx, tl in title_lines.items():
        box = boxes[bidx]
        rot = [x for x in tl if x.rotated]
        flat = [x for x in tl if not x.rotated]
        parts: list[str] = []
        if flat:
            flat.sort(key=lambda x: (round(x.bbox[1]), x.bbox[0]))
            parts.append(_join([x.text for x in flat]))
        if rot:
            # rotated title: columns of text; word fragments ('Cen','tral') are re-joined without a space
            dirs = rot[0].dir
            cols: dict[int, list] = {}
            for x in rot:
                cols.setdefault(round(x.bbox[0] / 3), []).append(x)
            texts = []
            for k in sorted(cols, reverse=dirs[1] > 0):
                frag = sorted(cols[k], key=lambda x: x.bbox[1] if dirs[1] > 0 else -x.bbox[1])
                s_ = ""
                for x in frag:
                    s_ = s_ + x.text if (s_ and (x.text[:1].islower() or s_.endswith("-"))) else (s_ + " " + x.text).strip()
                texts.append(s_)
            parts.append(_join(texts))
        title = _join(parts)
        box.title_lines = tl
        base = _INSTANCE.sub("", title).strip()
        box.title = re.sub(r"\s+", " ", base)
        if re.search(r"non[- ]?atomic|\bSW-?C\b", box.title, re.I) or len(box.title) > 70:
            box.title = ""

    _resolve_aliases(boxes)
    for p in ports:
        p.box = p.box.alias or p.box
    ports = [p for p in ports if p.box.title]

    # ---- signal labels -> arrows
    signals: list[SignalLabel] = []
    for ln in sig_lines:
        m = SIGNAL_LABEL.match(ln.text)
        sl = SignalLabel(name=m.group("name"), elem=m.group("elem").lower(), line=ln)
        cx0, cx1 = ln.bbox[0], ln.bbox[2]
        cy = (ln.bbox[1] + ln.bbox[3]) / 2
        best = None
        for ai, a in enumerate(arrows):
            for si in a["segs"]:
                x0, y0, x1, y1 = segs[si]
                if abs(y0 - y1) > 1.0:  # horizontal segments only carry labels
                    continue
                if min(x0, x1) - 3 > cx1 or max(x0, x1) + 3 < cx0:
                    continue
                d = abs(y0 - cy)
                # labels sit above (or on) the line: prefer a line below the label centre
                if y0 < cy - 2:
                    d += 3
                if d <= s.diag_label_arrow_dist and (best is None or d < best[0]):
                    best = (d, ai)
        if best:
            sl.arrow, sl.arrow_dist = best[1], best[0]
            a = arrows[sl.arrow]
            lc = _center(ln.bbox)
            for tp in sorted(a["tails"], key=lambda q: math.hypot(q[0] - lc[0], q[1] - lc[1])):
                sl.sender, sl.sender_dist = _end_box(boxes, [tp], ln)
                if sl.sender is not None:
                    break
            sl.receivers = [bd for bd in (_end_box(boxes, [h], None) for h in a["heads"]) if bd[0] is not None]
            if sl.sender is None:
                sl.sender_dist = 99.0
        signals.append(sl)

    # ---- port direction from arrow ends sitting at the port's y on the same border
    glyphs = _port_glyphs(page)
    for p in ports:
        pc = _center(p.line.bbox)
        edge = p.box.rect[0] if p.side == "left" else p.box.rect[2]
        for a in arrows:
            for kind, pts in (("provided", a["tails"]), ("required", a["heads"])):
                for pt in pts:
                    if abs(pt[1] - pc[1]) <= s.diag_port_arrow_y and abs(pt[0] - edge) <= s.diag_touch_tol + 3:
                        p.direction = kind
        glyph_direction = _glyph_direction(p, glyphs)
        if glyph_direction != "unknown":
            p.direction = glyph_direction if p.direction in ("unknown", glyph_direction) else "unknown"
    return PageDiagram(page=page_no, boxes=boxes, ports=ports, signals=signals, arrows=arrows,
                       markers=markers, segments=segs, heads=heads, height=page.rect.height)


def _resolve_aliases(boxes: list[Box]) -> None:
    """Untitled boxes that are stacked instances of / directly adjacent to a titled box act as that box."""
    for b in boxes:
        if b.title:
            continue
        for o in boxes:
            if not o.title or o is b:
                continue
            adj = (abs(b.rect[0] - o.rect[0]) <= 3 and abs(b.rect[2] - o.rect[2]) <= 3 and
                   (0 <= b.rect[1] - o.rect[3] <= 4 or 0 <= o.rect[1] - b.rect[3] <= 4))
            if _iou(b.rect, o.rect) > 0.5 or adj:
                b.alias = o
                break


def _join(parts: list[str]) -> str:
    out = ""
    seen: set[str] = set()
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        if not out:
            out = p
        elif out.endswith("-"):
            out += p
        else:
            out += " " + p
    return out.strip()


def _end_box(boxes, pts, ln):
    """Box whose border the arrow end touches (distance), smallest box wins; (None, 99) if none within tol."""
    s = settings
    best = None
    for pt in pts:
        cands = []
        for b0 in boxes:
            b = b0 if b0.title else b0.alias
            if b is None:
                continue
            d = _border_dist(pt, b0.rect)
            if d <= s.diag_touch_tol:
                cands.append((d, b.area, b))
        for d, area, b in cands:
            key = (round(d, 1) > s.diag_high_dist, area)
            if best is None or key < best[0]:
                best = (key, d, b)
    if best is None:
        return None, 99.0
    return best[2], best[1]


def analyze_pdf(path: str | Path, pages: list[int] | None = None) -> list[PageDiagram]:
    import pymupdf
    out = []
    with pymupdf.open(str(path)) as pdf:
        for i, page in enumerate(pdf):
            if pages and (i + 1) not in pages:
                continue
            pd = analyze_page(page, i + 1)
            if pd.boxes:
                out.append(pd)
    return out


# ---------------------------------------------------------------- entities

def find_pdf(doc: dict, extra_dirs: list | None = None) -> Path | None:
    """Locate the source PDF of a processed document by sha256 (raw + synthetic dirs) or `<doc_id>.pdf`."""
    dirs = list(extra_dirs or []) + [settings.raw_dir, settings.synthetic_dir]
    want = doc.get("sha256")
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        p = d / f"{doc['doc_id']}.pdf"
        if p.is_file() and (not want or hashlib.sha256(p.read_bytes()).hexdigest() == want):
            return p
        if want:
            for q in d.glob("*.pdf"):
                if hashlib.sha256(q.read_bytes()).hexdigest() == want:
                    return q
    return None


def _section_for(doc: dict, page_no: int, page, y_top: float) -> str:
    cand = [s for s in doc["sections"] if s["section_id"] != "0" and s["page_start"] <= page_no <= s["page_end"]]
    for s in reversed(cand):
        if s["page_start"] == page_no:
            hits = page.search_for(s["title"].strip()[:40])
            if hits and hits[0].y0 > y_top:
                continue  # heading is printed below the diagram
        return f"{s['section_id']} {s['title']}"
    return cand[-1] and f"{cand[-1]['section_id']} {cand[-1]['title']}" if cand else ""


def page_qualifies(pd: PageDiagram) -> bool:
    """A page is treated as an interface diagram only with >= 2 `Name / kind` signal labels or >= 3 port markers;
    conceptual block diagrams (chassis) are skipped instead of producing noise."""
    return len(pd.signals) >= 2 or len(pd.markers) >= 3


def extract_diagram_entities(doc: dict, pdf_path: str | Path) -> dict[str, list]:
    """Entities (components, ports, signals) from diagram geometry. Imports app.extract lazily."""
    import pymupdf

    from app.extract import Component, Port, Signal, Source, normalize_name
    s = settings
    comps: list = []
    ports: list = []
    sigs: list = []
    ocr_comps: list = []
    ocr_ports: list = []
    ocr_sigs: list = []
    doc_id, version = doc["doc_id"], doc.get("version")
    with pymupdf.open(str(pdf_path)) as pdf:
        vocab = doc_vocab(pdf)
        for i, page in enumerate(pdf):
            pd = analyze_page(page, i + 1, vocab)
            if not pd.boxes or not page_qualifies(pd):
                continue
            y_top = min(b.rect[1] for b in pd.boxes)
            sec = _section_for(doc, i + 1, page, y_top)
            # a page read only with the help of OCR'd labels: its sources are marked and its entities come last
            page_ocr = any(p.line.ocr for p in pd.ports) or any(sl.line.ocr for sl in pd.signals) \
                or any(ln.ocr for b in pd.boxes for ln in b.title_lines)
            src = Source(doc_id=doc_id, version=version, page=i + 1, section=sec, method="diagram", ocr=page_ocr)
            used_boxes: dict[int, Box] = {}
            pports = [p for p in pd.ports if p.box.title]
            for p in pports:
                used_boxes[p.box.idx] = p.box
            for sl in pd.signals:
                if sl.sender is not None:
                    used_boxes[sl.sender.idx] = sl.sender
                for b, _d in sl.receivers:
                    used_boxes[b.idx] = b
            high_by_box: dict[int, bool] = {}
            for p in pports:
                if p.gap <= s.diag_high_dist:
                    high_by_box[p.box.idx] = True
            for b in used_boxes.values():
                if not b.title or len(b.title) < 2:
                    continue
                conf = "High" if high_by_box.get(b.idx) and not page_ocr else "Medium"
                bsrc = src.model_copy(update={"evidence": b.title, "bbox": list(b.rect)})
                (ocr_comps if page_ocr else comps).append(Component(name=b.title, category=b.category, sources=[bsrc],
                                       description="Box drawn in the architecture diagram",
                                       extraction_method="diagram", confidence=conf))
            for p in pports:
                if len(p.name) < 3 or normalize_name(p.name) == normalize_name(p.box.title):
                    continue  # '[Instance]HMI' style label that only repeats the component name
                conf = "High" if p.gap <= s.diag_high_dist and not p.line.ocr and p.direction != "unknown" else "Medium"
                read = " (label read by OCR from an image)" if p.line.ocr else ""
                (ocr_ports if page_ocr else ports).append(Port(
                                  name=p.name, component=p.box.title, direction=p.direction,
                                  sources=[src.model_copy(update={"evidence": p.line.text, "bbox": list(p.line.bbox)})],
                                  description=f"Port label '{p.name}' on the {p.side} border of '{p.box.title}'{read}",
                                  extraction_method="diagram", confidence=conf))
            for sl in pd.signals:
                sender = sl.sender.title if sl.sender is not None else ""
                recv = []
                for b, d in sl.receivers:
                    if b.title and b.title not in recv:
                        recv.append(b.title)
                good = sl.arrow is not None and sl.arrow_dist <= s.diag_high_label_dist
                touch = all(d <= s.diag_high_dist for d in ([sl.sender_dist] if sl.sender else []) +
                            [d for _b, d in sl.receivers])
                conf = "High" if (good and sender and recv and touch) else "Medium"
                if sl.arrow is None or sl.line.ocr:
                    conf = "Medium"
                read = " (read by OCR from an image)" if sl.line.ocr else ""
                (ocr_sigs if page_ocr else sigs).append(Signal(
                                   name=sl.name, sender=sender, receivers=recv, data_type=sl.elem,
                                   sources=[src.model_copy(update={"evidence": sl.line.text, "bbox": list(sl.line.bbox)})],
                                   description=f"Diagram label {sl.name} / {sl.elem}{read}",
                                   extraction_method="diagram", confidence=conf))
    # OCR reads come last: when the same port/signal is also printed as text in another figure, merging keeps the
    # text-layer reading's page, sender and confidence and only adds the OCR source and receivers
    return {"components": comps + ocr_comps, "ports": ports + ocr_ports, "signals": sigs + ocr_sigs}


# ---------------------------------------------------------------- debug rendering

def render_debug(pdf_path: str | Path, page_no: int, out_png: str | Path, dpi: int = 130) -> dict:
    """Render the page with boxes (blue), titles, port labels (green=inside, orange=outside, with direction),
    signal labels (red) and arrows (magenta; tails o, heads x). Returns summary counts."""
    import pymupdf
    with pymupdf.open(str(pdf_path)) as pdf:
        page = pdf[page_no - 1]
        pd = analyze_page(page, page_no, doc_vocab(pdf))
        blue, green, orange, red, mag = (0, 0, 1), (0, 0.6, 0), (1, 0.5, 0), (1, 0, 0), (0.8, 0, 0.8)
        sh = page.new_shape()
        for b in pd.boxes:
            sh.draw_rect(pymupdf.Rect(*b.rect))
            sh.finish(color=blue, width=1.2, fill=None)
        sh.commit()
        for b in pd.boxes:
            if b.title:
                page.insert_text((b.rect[0] + 1, b.rect[1] - 2), f"#{b.idx} {b.title}", fontsize=6, color=blue)
        for p in pd.ports:
            r = pymupdf.Rect(*p.line.bbox)
            sh = page.new_shape()
            sh.draw_rect(r)
            sh.finish(color=green if p.inside else orange, width=0.8)
            sh.commit()
            tag = {"provided": "P", "required": "R", "unknown": "?"}[p.direction]
            page.insert_text((r.x0, r.y0 - 1), f"->#{p.box.idx}{tag}", fontsize=4.5, color=green if p.inside else orange)
        for a in pd.arrows:
            for si in a["segs"]:
                x0, y0, x1, y1 = pd.segments[si]
                page.draw_line((x0, y0), (x1, y1), color=mag, width=0.7)
            for t in a["tails"]:
                page.draw_circle(t, 2.2, color=mag, width=0.8)
            for h in a["heads"]:
                x, y = h
                page.draw_line((x - 2.5, y - 2.5), (x + 2.5, y + 2.5), color=mag, width=0.9)
                page.draw_line((x - 2.5, y + 2.5), (x + 2.5, y - 2.5), color=mag, width=0.9)
        for sl in pd.signals:
            r = pymupdf.Rect(*sl.line.bbox)
            sh = page.new_shape()
            sh.draw_rect(r)
            sh.finish(color=red, width=0.8)
            sh.commit()
            snd = f"#{sl.sender.idx}" if sl.sender else "?"
            rcv = ",".join(f"#{b.idx}" for b, _ in sl.receivers) or "?"
            page.insert_text((r.x0, r.y1 + 5), f"{snd}->{rcv}", fontsize=4.5, color=red)
        page.get_pixmap(dpi=dpi).save(str(out_png))
    return {"boxes": len(pd.boxes), "titled": sum(1 for b in pd.boxes if b.title), "ports": len(pd.ports),
            "signals": len(pd.signals), "arrows": len(pd.arrows)}
