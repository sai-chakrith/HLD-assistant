"""OCR for scanned PDF pages (pymupdf render at 300 dpi + Tesseract via pytesseract).

``ocr_page`` never raises: when OCR is disabled/unavailable it returns status "tesseract_missing" /
"disabled" / "error" with no lines, so the caller keeps the page with an empty-text flag.
Headings: OCR lines carry no bold/size, so a short "N Title" line (_HEAD, <=80 chars, starts uppercase,
no trailing period) is promoted to a heading (bold=True, size=14); everything else is body text.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import pymupdf

from app.config import settings

log = logging.getLogger(__name__)

DPI = 300
MIN_TEXT_CHARS = 20
_WIN_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
_HEAD = re.compile(r"^\d+(?:\.\d+){0,3}\s+[A-Z]\S.*$")
_warned = False


def is_scanned(page: "pymupdf.Page") -> bool:
    """True when the page has (almost) no text layer but has images."""
    return len(page.get_text().strip()) < MIN_TEXT_CHARS and bool(page.get_images())


def _cmd() -> str | None:
    if settings.tesseract_cmd:
        return settings.tesseract_cmd
    if os.name == "nt" and Path(_WIN_DEFAULT).is_file():
        return _WIN_DEFAULT
    return None


def tesseract_available() -> bool:
    """Configure pytesseract and check the binary runs."""
    try:
        import pytesseract
        cmd = _cmd()
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _warn_once(msg: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        log.warning(msg)


def ocr_page(page: "pymupdf.Page") -> dict[str, Any]:
    """OCR one page -> {status, lines, confidence, chars}; lines match ingest._page_lines (+ocr_conf)."""
    res: dict[str, Any] = {"status": "ok", "lines": [], "confidence": None, "chars": 0}
    if not settings.ocr_enabled:
        res["status"] = "disabled"
        return res
    if not tesseract_available():
        _warn_once("Tesseract not found: scanned pages are kept with empty text. Install it "
                   "(https://github.com/UB-Mannheim/tesseract/wiki) and set TESSERACT_CMD or add it to PATH.")
        res["status"] = "tesseract_missing"
        return res
    try:
        import pytesseract
        from PIL import Image
        pix = page.get_pixmap(dpi=DPI)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    except Exception as e:
        log.warning("OCR failed on page: %s", e)
        res["status"] = "error"
        return res
    k = 72.0 / DPI
    groups: dict[tuple, list[int]] = {}
    for i, w in enumerate(d["text"]):
        if w.strip() and float(d["conf"][i]) >= 0:
            groups.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(i)
    confs: list[float] = []
    for (blk, _p, _l), idx in sorted(groups.items(), key=lambda kv: (d["top"][kv[1][0]], d["left"][kv[1][0]])):
        text = re.sub(r"\s+", " ", " ".join(d["text"][i] for i in idx)).strip()
        c = sum(float(d["conf"][i]) for i in idx) / len(idx)
        x0, y0 = min(d["left"][i] for i in idx), min(d["top"][i] for i in idx)
        x1 = max(d["left"][i] + d["width"][i] for i in idx)
        y1 = max(d["top"][i] + d["height"][i] for i in idx)
        head = bool(_HEAD.match(text)) and len(text) <= 80 and not text.endswith(".")
        res["lines"].append({"text": text, "size": 14.0 if head else 10.0, "bold": head,
                             "bbox": (x0 * k, y0 * k, x1 * k, y1 * k), "block": blk, "ocr_conf": c})
        confs.extend(float(d["conf"][i]) for i in idx)
    res["chars"] = sum(len(ln["text"]) for ln in res["lines"])
    res["confidence"] = round(sum(confs) / len(confs), 1) if confs else None
    return res
