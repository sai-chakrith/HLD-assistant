"""Build data/synthetic/scanned_sample.pdf: pages 6 (port table) and 8 (functional flows) of hld_v1.pdf
rasterised to grayscale images with NO text layer (a stand-in for a scanned document).

Usage: python data/synthetic/make_scanned_sample.py
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
PAGES = (6, 8)  # 1-based pages of hld_v1.pdf
DPI = 170


def build(src: Path = HERE / "hld_v1.pdf", out: Path = HERE / "scanned_sample.pdf") -> Path:
    with pymupdf.open(str(src)) as s, pymupdf.open() as o:
        for n in PAGES:
            pg = s[n - 1]
            pix = pg.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
            new = o.new_page(width=pg.rect.width, height=pg.rect.height)
            new.insert_image(new.rect, stream=pix.tobytes("png"))
        o.save(str(out), garbage=4, deflate=True)
    return out


if __name__ == "__main__":
    p = build()
    print(p, p.stat().st_size, "bytes")
