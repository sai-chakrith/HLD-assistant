"""Download the real AUTOSAR PDFs into data/raw/ and write manifest.json."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pymupdf as fitz  # PyMuPDF
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402

B = "https://www.autosar.org/fileadmin/standards"
SOURCES = [
    {"doc_id": "bodycomfort_r3.1.5", "title": "Explanation of Application Interfaces of the Body and Comfort Domain",
     "domain": "body-comfort", "release": "R3.1.5", "project_id": "body-comfort",
     "source_url": f"{B}/R3.1.5/CP/AUTOSAR_ApplicationInterfaces_Explanation_BodyComfort.pdf"},
    {"doc_id": "bodycomfort_r3.2.3", "title": "Explanation of Application Interfaces of the Body and Comfort Domain",
     "domain": "body-comfort", "release": "R3.2.3", "project_id": "body-comfort",
     "source_url": f"{B}/R3.2.3/CP/AUTOSAR_ApplicationInterfaces_Explanation_BodyComfort.pdf"},
    {"doc_id": "chassis_r3.1.5", "title": "Explanation of Application Interfaces of the Chassis Domain",
     "domain": "chassis", "release": "R3.1.5", "project_id": "chassis",
     "source_url": f"{B}/R3.1.5/CP/AUTOSAR_ApplicationInterfaces_Explanation_Chassis.pdf"},
    {"doc_id": "chassis_r3.2.3", "title": "Explanation of Application Interfaces of the Chassis Domain",
     "domain": "chassis", "release": "R3.2.3", "project_id": "chassis",
     "source_url": f"{B}/R3.2.3/CP/AUTOSAR_ApplicationInterfaces_Explanation_Chassis.pdf"},
    {"doc_id": "hmi_r25-11", "title": "Explanation of Application Interfaces of the HMI, Multimedia and Telematics Domain",
     "domain": "hmi", "release": "R25-11", "project_id": "hmi",
     "source_url": f"{B}/R25-11/CP/AUTOSAR_CP_EXP_AIHMIMultimediaAndTelematics.pdf"},
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def validate_pdf(path: Path) -> int:
    """Return page count if path is a valid PDF (header + opens with PyMuPDF); raise ValueError otherwise."""
    with path.open("rb") as f:
        if not f.read(1024).lstrip().startswith(b"%PDF"):
            raise ValueError(f"{path.name}: missing %PDF header")
    try:
        with fitz.open(path) as doc:
            pages = doc.page_count
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{path.name}: cannot open with PyMuPDF: {exc}") from exc
    if pages < 1:
        raise ValueError(f"{path.name}: zero pages")
    return pages


def build_manifest_entry(src: dict, path: Path) -> dict:
    pages = validate_pdf(path)
    return {**src, "filename": path.name, "sha256": sha256_of(path), "pages": pages,
            "size_bytes": path.stat().st_size, "source_status": "approved"}


def write_manifest(entries: list[dict], raw_dir: Path) -> Path:
    out = raw_dir / "manifest.json"
    out.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return out


def download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(".part")
    try:
        with requests.get(url, stream=True, timeout=60, headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for block in r.iter_content(1 << 20):
                    f.write(block)
    except requests.exceptions.SSLError:
        # autosar.org serves an incomplete certificate chain; Python's bundled CAs cannot complete it but the OS trust
        # store (used by curl) can. Verification stays ON; this only switches the client.
        import shutil
        import subprocess
        if not shutil.which("curl"):
            raise
        print("  [ssl ] python could not verify the chain, retrying with curl (certificate still verified)")
        subprocess.run(["curl", "-sSL", "--fail", "-A", "Mozilla/5.0", "-o", str(tmp), url], check=True, timeout=300)
    tmp.replace(dest)


def main() -> int:
    raw = settings.raw_dir
    raw.mkdir(parents=True, exist_ok=True)
    entries, failures = [], []
    for src in SOURCES:
        dest = raw / f"{src['doc_id']}.pdf"
        try:
            if dest.exists():
                print(f"[skip] {dest.name} exists")
            else:
                print(f"[get ] {src['source_url']}")
                download(src["source_url"], dest)
            entry = build_manifest_entry(src, dest)
            print(f"[ ok ] {dest.name}: {entry['size_bytes'] / 1e6:.2f} MB, {entry['pages']} pages")
            entries.append(entry)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {src['doc_id']}: {exc}")
            failures.append(src["doc_id"])
            if dest.exists() and not dest.stat().st_size:
                dest.unlink()
    print("manifest:", write_manifest(entries, raw))
    if failures:
        print("failed:", ", ".join(failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
