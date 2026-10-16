"""Acceptance run over every component, real runs (uvicorn + Ollama + bge), on a scratch copy of the live data
(eval/_work/acceptance) so nothing in data/ changes. 17 rows, each PASS/FAIL with evidence; the table is appended to
eval/run.log and stored under "acceptance" in eval/results.json / eval/results.md.

    .venv\\Scripts\\python -m eval.acceptance_check [--rows 1 2 ...] [--skip-slow]

--skip-slow leaves out rows 15b (demo_reset from a clean state), 16 (pytest -m llm) and 17 (gold re-runs); those can
run on their own with --rows 15 16 17.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from eval.e2e_check import BASE_ENV, _check_file, _cites, loaded  # noqa: E402
from eval.run_eval import (EVAL_DIR, WORK, GpuMonitor, lat_stats, ms,  # noqa: E402
                           pct, table, trace_entries, unload_all)

SCRATCH = "acc-scratch"
W = WORK / "acceptance"
A, R, V, O = ({"X-User": u} for u in ("admin", "reviewer", "viewer", "outsider"))
ROWS: dict[int, dict] = {}
CTX: dict = {}  # values shared between rows (server base, uploaded doc ids, ...)
CHROME = sorted(Path.home().glob("AppData/Local/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell.exe"))


def log(line: str) -> None:
    print(line, flush=True)
    with open(EVAL_DIR / "run.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def row(n: int, name: str, ok: bool, evidence: str, seconds: float | None = None) -> None:
    ROWS[n] = {"row": n, "name": name, "ok": bool(ok), "evidence": evidence, "seconds": seconds}
    log(f"[{'PASS' if ok else 'FAIL'}] {n:>2}. {name}: {evidence}" + (f" ({seconds:.1f} s)" if seconds else ""))


def note(line: str) -> None:
    log(f"     {line}")


# ---------------------------------------------------------------- scratch data + server
def prepare() -> None:
    from app import controls
    from app.config import settings
    shutil.rmtree(W, ignore_errors=True)
    W.mkdir(parents=True)
    shutil.copy(settings.db_path, W / "hld.db")
    shutil.copytree(settings.chroma_dir, W / "chroma")
    shutil.copytree(settings.processed_dir, W / "processed")
    c = controls.get_conn(W / "hld.db")
    controls.create_project(SCRATCH, "Acceptance scratch project", conn=c)
    for u, r in (("admin", "admin"), ("reviewer", "reviewer"), ("viewer", "viewer")):
        controls.add_member(SCRATCH, u, r, conn=c)
    controls.add_user("outsider", "viewer", conn=c)  # a user with no project at all
    if c.execute("SELECT name FROM sqlite_master WHERE name='llm_cache'").fetchone():
        c.execute("DELETE FROM llm_cache")  # first calls are real: honest latencies and model swaps
        c.commit()
    c.close()


class Server:
    def __init__(self, offline: bool = False, extra: dict | None = None):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        env = {**BASE_ENV, "DB_PATH": str(W / "hld.db"), "CHROMA_DIR": str(W / "chroma"),
               "PROCESSED_DIR": str(W / "processed"), "WARMUP": "1", "PYTHONIOENCODING": "utf-8", **(extra or {})}
        if offline:
            env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        self.t0 = time.perf_counter()
        self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.api:app", "--port", str(self.port)],
                                     cwd=ROOT, env=env, stdout=open(W / f"uvicorn_{self.port}.log", "w"),
                                     stderr=subprocess.STDOUT)

    def wait_ready(self) -> tuple[float, dict]:
        import httpx
        while True:
            try:
                h = httpx.get(self.base + "/health", timeout=5).json()
                if h["warmup"]["status"] != "warming":
                    return time.perf_counter() - self.t0, h
            except httpx.HTTPError:
                pass
            if time.perf_counter() - self.t0 > 600 or self.proc.poll() is not None:
                raise SystemExit("server did not become ready")
            time.sleep(0.25)

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=30)


def http():
    import httpx
    return httpx.Client(base_url=CTX["base"], timeout=3600)


def upload(c, name: str, content: bytes, status: str = "approved", use_llm: bool = False, user=A, pid=SCRATCH):
    return c.post(f"/projects/{pid}/upload", headers=user, files={"file": (name, content, "application/pdf")},
                  data={"doc_type": "hld", "version": "1.0", "source_status": status, "use_llm": str(use_llm).lower()})


def ask(c, pid: str, q: str, user=V) -> dict:
    t = time.perf_counter()
    r = c.post(f"/projects/{pid}/ask", headers=user, json={"question": q})
    a = r.json() if r.status_code == 200 else {"mode": f"HTTP {r.status_code}", "answer": r.text[:200]}
    a["_ms"], a["_status"] = (time.perf_counter() - t) * 1000, r.status_code
    return a


# ---------------------------------------------------------------- rows
def row1_environment(h: dict) -> None:
    out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=60).stdout
    have = {ln.split()[0] for ln in out.splitlines()[1:] if ln.strip()}
    need = {"qwen3:4b-instruct", "qwen2.5:3b"}
    emb = h.get("embedder") or {}
    db = sqlite3.connect(W / "hld.db")
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    db.close()
    chroma_files = sum(1 for _ in (W / "chroma").rglob("*") if _.is_file())
    ok = need <= have and h["warmup"]["status"] == "ready" and not emb.get("fallback") and emb.get("kind") == "bge" \
        and integrity == "ok" and chroma_files > 0
    row(1, "Environment", ok, f"ollama list has {sorted(need & have)} (missing {sorted(need - have) or 'none'}); /health "
        f"{h['status']}, warm-up {h['warmup']['status']}; embedder {emb.get('kind')} ({emb.get('model')}) fallback="
        f"{emb.get('fallback')}; SQLite integrity {integrity}; chroma {chroma_files} files")


def row2_ingestion() -> None:
    import pymupdf
    from app.config import settings
    t = time.perf_counter()
    c = http()
    real = (settings.raw_dir / "chassis_r3.2.3.pdf").read_bytes()
    syn = (settings.synthetic_dir / "hld_v1.pdf").read_bytes()
    r1 = upload(c, "acc_chassis_small.pdf", real, "approved", use_llm=False)
    r2 = upload(c, "acc_syn_v1.pdf", syn, "synthetic", use_llm=True)
    CTX["real_doc"], CTX["syn_doc"] = "acc_chassis_small", "acc_syn_v1"
    CTX["syn_upload"] = r2.json() if r2.status_code == 200 else {}
    facts = []
    for doc in (CTX["real_doc"], CTX["syn_doc"]):
        d = json.loads((W / "processed" / SCRATCH / f"{doc}.json").read_text(encoding="utf-8"))
        n_tables = sum(len(s.get("tables", [])) for s in d["sections"])
        facts.append(f"{doc}: {d.get('n_pages')} pages, {len(d['sections'])} sections, {n_tables} tables, metadata "
                     f"{sorted(d.get('metadata', {}))[:4]}, {len(d.get('revisions', []))} change-history rows")
    import chromadb
    from chromadb.config import Settings as CS

    from app.retrieve import collection_name
    cl = chromadb.PersistentClient(path=str(W / "chroma"), settings=CS(anonymized_telemetry=False))
    col = cl.get_collection(collection_name(SCRATCH, "bge"))
    chunks = {d: len(col.get(where={"doc_id": d})["ids"]) for d in (CTX["real_doc"], CTX["syn_doc"])}
    # rejections
    tmp = W / "locked.pdf"
    with pymupdf.open(stream=syn, filetype="pdf") as d:
        d.save(str(tmp), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    big = syn + b"\n%" + b"0" * int((settings.max_upload_mb + 1) * 1e6)
    rejects = {}
    for label, name, content, status in (
            ("draft", "acc_draft.pdf", syn + b"\n%draft", "draft"),
            ("non-PDF", "notes.txt", b"just text", "approved"),
            ("duplicate", "acc_syn_v1_copy.pdf", syn, "synthetic"),
            ("oversized", "acc_big.pdf", big, "approved"),
            ("path traversal", "..\\x.pdf", syn + b"\n%t", "approved"),
            ("password-protected", "acc_locked.pdf", tmp.read_bytes(), "approved")):
        rr = upload(c, name, content, status)
        rejects[label] = (rr.status_code, (rr.json().get("detail", "") if rr.status_code != 200 else "ACCEPTED")[:60])
    ok = r1.status_code == 200 and r2.status_code == 200 and all(v[0] == 400 for v in rejects.values()) \
        and all(chunks.values())
    row(2, "Ingestion", ok, f"real upload HTTP {r1.status_code}, synthetic HTTP {r2.status_code}; " + "; ".join(facts)
        + f"; chunks {chunks}; rejected: " + "; ".join(f"{k} {v[0]} '{v[1]}'" for k, v in rejects.items()),
        time.perf_counter() - t)


def row3_ocr() -> None:
    from app.config import settings
    t = time.perf_counter()
    c = http()
    r = upload(c, "acc_scanned.pdf", (settings.synthetic_dir / "scanned_sample.pdf").read_bytes(), "synthetic")
    d = json.loads((W / "processed" / SCRATCH / "acc_scanned.json").read_text(encoding="utf-8")) \
        if r.status_code == 200 else {"ocr_pages": [], "sections": []}
    pages = d.get("ocr_pages", [])
    text = sum(len(s.get("text", "")) for s in d["sections"])
    import chromadb
    from chromadb.config import Settings as CS

    from app.retrieve import collection_name
    cl = chromadb.PersistentClient(path=str(W / "chroma"), settings=CS(anonymized_telemetry=False))
    metas = cl.get_collection(collection_name(SCRATCH, "bge")).get(where={"doc_id": "acc_scanned"})["metadatas"]
    flagged = sum(1 for m in metas if m.get("ocr"))
    a = ask(c, SCRATCH, "Which component provides the ISwitchCmd interface and what are the steps of manual window "
                        "operation?")
    scanned_cited = [x for x in a.get("citations", []) if x["doc_id"] == "acc_scanned"]
    ocr_note = "OCR" in (a.get("limitation") or "") or any(x.get("ocr") for x in a.get("citations", []))
    ok = r.status_code == 200 and pages and all(p["status"] == "ok" for p in pages) and text > 200 and flagged \
        and a.get("mode") == "generated" and scanned_cited and ocr_note
    row(3, "OCR", ok, f"HTTP {r.status_code}; OCR pages {[(p['page'], p['status'], p.get('confidence')) for p in pages]}; "
        f"{text} chars of text; {flagged}/{len(metas)} chunks flagged ocr; question {a.get('mode')} citing "
        f"{_cites(a)}; OCR note in answer: {ocr_note} ('{(a.get('limitation') or '')[-110:]}')", time.perf_counter() - t)


def row4_extraction() -> None:
    from app.extract import is_junk_name
    t = time.perf_counter()
    c = http()
    parts, junk, truncated, ok = [], [], [], True
    for pid, doc in (("body-comfort", "bodycomfort_r3.2.3"), ("chassis", "chassis_r3.2.3"), ("hmi", "hmi_r25-11"),
                     ("synthetic-ecu", "syn_hld_v1")):
        inv = c.get(f"/projects/{pid}/inventory", headers=V, params={"doc_id": doc}).json()
        counts = {t_: len(v) for t_, v in inv.items()}
        methods = {}
        for t_, v in inv.items():
            for e in v:
                methods[e["extraction_method"]] = methods.get(e["extraction_method"], 0) + 1
                if re.fullmatch(r"\w+::\w*\.\.\.", e["name"]):  # 'Inst::Type...' truncated by the figure itself
                    truncated.append(f"{doc}:{e['name']}")
                elif is_junk_name(e["name"]) or len(e["name"].split()) > 6 or re.search(r"[.;:]\s*$", e["name"]):
                    junk.append(f"{doc}:{e['name']}")
        parts.append(f"{doc} {counts} by {methods}")
        ok &= sum(counts.values()) > 0
    bc = c.get("/projects/body-comfort/inventory", headers=V, params={"doc_id": "bodycomfort_r3.2.3"}).json()
    ww = [x for x in bc["components"] if x["name"] == "WiperWasher"]
    p41 = [p for p in bc["ports"] if (p.get("section") or "").startswith("4.1 ")]
    # review decision survives a re-extraction (scratch copy, approved row)
    sinv = c.get(f"/projects/{SCRATCH}/inventory", headers=V, params={"doc_id": CTX["syn_doc"]}).json()
    port = sinv["ports"][0]
    rv = c.post(f"/projects/{SCRATCH}/review", headers=R, json={"item_type": "port", "item_id": port["id"],
                                                                 "decision": "approved", "comment": "acceptance"})
    rx = c.post(f"/projects/{SCRATCH}/extract", headers=A, json={"doc_id": CTX["syn_doc"], "use_llm": False})
    after = c.get(f"/projects/{SCRATCH}/inventory", headers=V, params={"doc_id": CTX["syn_doc"]}).json()
    same = [p for p in after["ports"] if p["id"] == port["id"]]
    kept = bool(same) and same[0]["review_status"] == "approved"
    ok &= not junk and kept and rv.status_code == 200 and rx.status_code == 200 and bool(ww)
    row(4, "Extraction", ok, "; ".join(parts) + f"; junk names: {junk[:5] or 'none'}; labels truncated in the source "
        f"figure, kept as printed (lookups use the instance part): {truncated or 'none'}; WiperWasher composition children "
        f"{(ww[0].get('children') if ww else None)}; section 4.1 ports {len(p41)} (raster figure: documented "
        f"limitation); approved port {port['name']} id {port['id']} after re-extraction (HTTP {rx.status_code}): "
        f"{'still approved, same id' if kept else 'LOST'}", time.perf_counter() - t)


def row5_checks() -> None:
    sys.path.insert(0, str(ROOT))
    from scripts.eval_checks import evaluate

    from app import checks, controls
    t = time.perf_counter()
    gt = json.loads((ROOT / "data/synthetic/ground_truth.json").read_text(encoding="utf-8"))
    planted = [x for x in gt["planted_defects"] if x["version"] == "1.0"]
    conn = controls.get_conn(W / "hld.db")
    fs = checks.get_findings(SCRATCH, CTX["syn_doc"], None, None, conn)
    real = checks.get_findings("body-comfort", "bodycomfort_r3.2.3", None, None, conn)
    conn.close()
    res = evaluate(fs, planted)
    by_rule = {r_: sum(1 for f in fs if f.rule_id == r_) for r_ in ("M1", "M2", "M3", "M4", "C1", "C2", "C3", "C4")}
    potential = sum(1 for f in real if "potential" in (f.label or ""))
    ok = all(by_rule.values()) and not res["missed"] and real and potential == len(real)
    row(5, "Checks", ok, f"synthetic v1 (scratch upload) findings per rule {by_rule}; planted defects found "
        f"{len(res['found'])}/{len(planted)}, missed {res['missed'] or 'none'}; real BodyComfort findings {len(real)}, "
        f"labelled 'potential, needs review': {potential}/{len(real)}", time.perf_counter() - t)


def row6_compare() -> None:
    t = time.perf_counter()
    c = http()
    parts, ok = [], True
    CTX["diff_exports"] = []
    for pid, a_, b_ in (("synthetic-ecu", "syn_hld_v1", "syn_hld_v2"), ("body-comfort", "bodycomfort_r3.1.5",
                                                                      "bodycomfort_r3.2.3"),
                        ("chassis", "chassis_r3.1.5", "chassis_r3.2.3")):
        d = c.post(f"/projects/{pid}/compare", headers=V, json={"doc_a": a_, "doc_b": b_}).json()
        ent = {k: {x: len(d[k][x]) for x in ("added", "removed", "changed")} for k in
               ("components", "ports", "interfaces", "signals")}
        sec = (d.get("sections") or {}).get("summary")
        exp = []
        for fmt in ("xlsx", "csv", "json"):
            rr = c.get(f"/projects/{pid}/export/diff", headers=V, params={"doc_a": a_, "doc_b": b_, "fmt": fmt})
            name = rr.headers.get("content-disposition", "").split("filename=")[-1].strip('"') or f"x.{fmt}"
            okf, _ = _check_file(rr.content, name, fmt) if rr.status_code == 200 else (False, "")
            exp.append(f"{fmt} {'ok' if okf else 'FAIL'}")
            CTX["diff_exports"].append((f"{a_}->{b_}.{fmt}", rr.status_code, rr.content, name, fmt))
            ok &= okf
        ok &= sec is not None
        parts.append(f"{a_}->{b_}: entities {ent}; sections {sec}; diff export {', '.join(exp)}")
    row(6, "Compare", ok, " | ".join(parts), time.perf_counter() - t)


def row7_graph() -> None:
    t = time.perf_counter()
    c = http()
    g = c.get("/projects/body-comfort/graph", headers=V, params={"doc_id": "bodycomfort_r3.2.3"}).json()
    ext = re.findall(r"<(?:script|link)[^>]+(?:src|href)=[\"']https?://[^\"']+", g["html"])
    f = c.get("/projects/body-comfort/graph", headers=V, params={"doc_id": "bodycomfort_r3.2.3",
                                                                 "component": "InteriorLightManager", "depth": 1}).json()
    ok = g["nodes"] > 0 and g["edges"] > 0 and not ext and 0 < f["nodes"] < g["nodes"]
    row(7, "Graph", ok, f"BodyComfort R3.2.3: {g['nodes']} nodes, {g['edges']} edges, {len(g['components'])} components; "
        f"external scripts/styles in the HTML: {len(ext)}; focus InteriorLightManager depth 1: {f['nodes']} nodes, "
        f"{f['edges']} edges", time.perf_counter() - t)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text or "") if re.search(r"\w", s)]


def row8_reports() -> None:
    t = time.perf_counter()
    c = http()
    parts, ok, CTX["reports"] = [], True, []
    for pid, doc, comp in (("body-comfort", "bodycomfort_r3.2.3", "InteriorLightManager"),
                           ("body-comfort", "bodycomfort_r3.2.3", "MirrorAdjust Manager"),
                           ("synthetic-ecu", "syn_hld_v1", "WinCtrlManager")):
        r = c.get(f"/projects/{pid}/reports/component", headers=V, params={"doc_id": doc, "component": comp})
        rep = r.json() if r.status_code == 200 else {}
        sents = _sentences(rep.get("purpose", ""))
        uncited = [s for s in sents if not re.search(r"\[[A-Z]\d+(?:\s*,\s*[A-Z]\d+)*\]", s)]
        ok &= r.status_code == 200 and bool(sents) and not uncited
        parts.append(f"{comp}: {len(sents)} purpose sentences, uncited {len(uncited)}, model {rep.get('model')}, "
                     f"confidence {rep.get('confidence')}, limitation '{(rep.get('limitation') or '-')[:60]}'")
        if rep.get("id"):
            CTX["reports"].append((pid, rep["id"]))
    for pid, doc in (("body-comfort", "bodycomfort_r3.2.3"), ("synthetic-ecu", "syn_hld_v1")):
        fl = c.get(f"/projects/{pid}/reports/flow", headers=V, params={"doc_id": doc}).json()
        steps = [s for f_ in fl.get("flows", []) for s in f_.get("steps", [])]
        uncited = [s for s in steps if not s.get("citation")]
        ok &= not uncited
        parts.append(f"flow summary {doc}: {len(fl.get('flows', []))} flows, {len(steps)} steps, uncited {len(uncited)}")
        if fl.get("id"):
            CTX["reports"].append((pid, fl["id"]))
    row(8, "Reports", ok, "; ".join(parts) + " (uncited sentences are removed before display: the limitation says "
        "so when it happens)", time.perf_counter() - t)


QA = [  # (label, project, question, expectation)
    ("ports of a named component", "body-comfort", "Which ports does the InteriorLightManager have?", "inventory_list"),
    ("short acronym component", "chassis", "What does the ACC SW-Composition control?", "answer:ACC"),
    ("section question", "body-comfort", "What does section 4.3 Interior Light describe?", "answer"),
    ("who sends / receives", "synthetic-ecu", "Who sends SwitchRequest and who receives it?", "inventory_list"),
    ("change / release", "body-comfort", "What changed between release R3.1.5 and R3.2.3?", "answer"),
    ("exact identifier", "synthetic-ecu", "What is the MotorCommand signal used for?", "answer:MotorCommand"),
    ("made-up component", "body-comfort", "Which ports does the FluxCapacitorManager have?", "refused"),
    ("off-topic", "body-comfort", "What is a good recipe for pancakes with blueberries?", "refused:off_topic"),
    ("on-topic but missing", "synthetic-ecu", "What ASIL level is assigned to the WinCtrlManager?", "refused"),
    ("cross-project", "chassis", "Which ports does the InteriorLightManager have?", "refused:no_leak"),
    ("repeated (cache)", "body-comfort", "What does section 4.2.1 Mirror Adjustment describe?", "answer:cached"),
    ("long question", "body-comfort",
     "In the Body and Comfort domain document, release R3.2.3, I am reviewing the interior light function and need "
     "to understand how the InteriorLightManager interacts with the other software components: which ports does it "
     "require from the HMI and from the door and key related components, and which signals does it receive, "
     "including the page of the architecture figure where these connections are drawn?", "answer")]


def _page_text(pid: str, doc: str, page: int) -> str:
    import pymupdf

    from app import diagram
    from app.ingest import load_processed
    own = W / "processed" / pid / "pdfs" / f"{doc}.pdf"
    path = own if own.is_file() else diagram.find_pdf(load_processed(pid, doc, W / "processed"))
    with pymupdf.open(str(path)) as p:
        return p[page - 1].get_text() if path and 0 < page <= p.page_count else ""


def _grounded(a: dict, pid: str) -> str:
    """Identifiers of the answer that occur on (one of) its cited pages: 'k/n' (automated page inspection)."""
    idents = set(re.findall(r"\b[A-Z][a-z]+[A-Z][A-Za-z0-9]+\b", a.get("answer", "")))
    if not idents or not a.get("citations"):
        return "-"
    text = " ".join(_page_text(pid, x["doc_id"], p) for x in a["citations"]
                    for p in range(x["page_start"] or 0, (x["page_end"] or x["page_start"] or 0) + 1) if p)
    flat = re.sub(r"\s+", "", text)
    hit = sum(1 for i in idents if i in text or i in flat)
    return f"{hit}/{len(idents)}"


def _qa_ok(want: str, a: dict, first: dict | None, bc_docs: set) -> bool:
    mode, kind = a.get("mode"), want.split(":")[0]
    if kind == "inventory_list":
        return mode == "generated" and a.get("answer_type") == "inventory_list" and bool(a.get("citations"))
    if kind == "answer":
        extra = want.split(":")[1] if ":" in want else ""
        good = mode == "generated" and bool(a.get("citations"))
        if extra == "cached":
            return good and first is not None and a["answer"] == first["answer"] and a["_ms"] < first["_ms"]
        return good and (not extra or extra in a.get("answer", ""))
    if want == "refused:off_topic":
        return mode == "refused" and a.get("refusal_reason") == "off_topic"
    if want == "refused:no_leak":
        return mode == "refused" and not any(x["doc_id"] in bc_docs for x in a.get("citations", [])) \
            and "DashBoardBrightness" not in a.get("answer", "")
    return mode == "refused"


def row9_qa() -> None:
    from app.config import settings
    t = time.perf_counter()
    c = http()
    bc_docs = {"bodycomfort_r3.1.5", "bodycomfort_r3.2.3"}
    rows, ok, first = [], True, None
    for i, (label, pid, q, want) in enumerate(QA):
        if label == "repeated (cache)":  # first ask of a new question (must not be a cache hit), then the repeat
            off = len(trace_entries(settings.trace_path))
            first = ask(c, pid, q)
            ftr = [e for e in trace_entries(settings.trace_path)[off:] if e.get("purpose") in ("qa", "qa_desc")]
            first["_cached"] = bool(ftr) and all(e.get("cached") for e in ftr)
            CTX["repeat_first"] = first
        off = len(trace_entries(settings.trace_path))
        a = ask(c, pid, q)
        tr = [e for e in trace_entries(settings.trace_path)[off:] if e.get("purpose") in ("qa", "qa_desc")]
        cached = bool(tr) and all(e.get("cached") for e in tr)
        good = _qa_ok(want, a, first, bc_docs)
        if label == "repeated (cache)":
            good = good and not first["_cached"] and cached
        ok &= good
        r_ = {"label": label, "project": pid, "question": q, "ok": good, "mode": a.get("mode"),
              "reason": a.get("refusal_reason") or "", "type": a.get("answer_type"), "built_from": a.get("built_from"),
              "confidence": a.get("confidence"), "model": a.get("model") or "-", "ms": a["_ms"], "llm_cached": cached,
              "citations": _cites(a), "grounded": _grounded(a, pid), "answer": a.get("answer", "")[:220]}
        rows.append(r_)
        note(f"9.{i + 1:<2} {label}: {'ok' if good else 'NOT OK'} {r_['mode']} {r_['reason']} type={r_['type']} "
             f"conf={r_['confidence']} model={r_['model']} {a['_ms'] / 1000:.1f}s cached={cached} "
             f"cites=[{r_['citations']}] ids-on-cited-page={r_['grounded']} | {r_['answer'][:150]}")
    CTX["qa_rows"] = rows
    lat = lat_stats([r_["ms"] for r_ in rows])
    rep, rep_first = rows[10], CTX.get("repeat_first", {})
    ui = _qa_ui(rows)
    ok &= ui["ok"]
    row(9, "Q&A (API + UI)", ok, f"API {sum(r_['ok'] for r_ in rows)}/12 as expected; latency p50 {ms(lat['p50_ms'])} "
        f"p95 {ms(lat['p95_ms'])} (LLM cache emptied first: all first asks are real calls); repeated question "
        f"{ms(rep['ms'])} vs first ask {ms(rep_first.get('_ms'))} (first cached {rep_first.get('_cached')}, repeat "
        f"cached {rep['llm_cached']}, same answer {(rep_first.get('answer') or '')[:220] == rep.get('answer')}); "
        f"UI {ui['summary']}", time.perf_counter() - t)


def _qa_ui(rows: list[dict]) -> dict:
    """The same 12 questions typed into the Ask page: the rendered result agrees with the API (refused or not)."""
    if not CHROME:
        return {"ok": False, "summary": "no Chromium for Playwright"}
    from playwright.sync_api import sync_playwright
    agree, errs = 0, []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=str(CHROME[-1]))
        ctx = b.new_context(viewport={"width": 1280, "height": 900})
        ctx.add_cookies([{"name": "hld_user", "value": "viewer", "url": CTX["base"]}])
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)))
        for r_ in rows:
            pg.goto(f"{CTX['base']}/?project={r_['project']}")
            pg.fill("#question", r_["question"])
            pg.click("#ask-btn")
            pg.wait_for_function("() => !document.getElementById('ask-btn').disabled", timeout=600000)
            refused_ui = pg.locator("#answer .answer.refused").count() > 0
            agree += refused_ui == (r_["mode"] == "refused")
        b.close()
    return {"ok": agree == len(rows) and not errs,
            "summary": f"{agree}/{len(rows)} rendered the same outcome as the API, page errors {len(errs)}"}


def row10_roles() -> None:
    t = time.perf_counter()
    c = http()
    syn = (ROOT / "data/synthetic/hld_v2.pdf").read_bytes()
    fid = c.get(f"/projects/{SCRATCH}/findings", headers=V, params={"doc_id": CTX["syn_doc"]}).json()[0]["id"]
    codes = {
        "viewer approve": c.post(f"/projects/{SCRATCH}/review", headers=V, json={
            "item_type": "finding", "item_id": fid, "decision": "approved"}).status_code,
        "viewer upload (POST /projects/{pid}/upload)": upload(c, "acc_v2.pdf", syn, "synthetic", user=V).status_code,
        "viewer audit": c.get("/audit", headers=V).status_code,
        "reviewer approve": c.post(f"/projects/{SCRATCH}/review", headers=R, json={
            "item_type": "finding", "item_id": fid, "decision": "approved", "comment": "acceptance"}).status_code,
        "admin upload": upload(c, "acc_v2.pdf", syn, "synthetic", user=A).status_code,
        "non-member documents": c.get(f"/projects/{SCRATCH}/documents", headers=O).status_code,
        "non-member ask": c.post(f"/projects/{SCRATCH}/ask", headers=O, json={"question": "x"}).status_code,
        "non-member upload": upload(c, "acc_v3.pdf", syn + b"%x", "synthetic", user=O).status_code}
    audit = c.get("/audit", headers=A, params={"limit": 1000}).json()
    acts = {}
    for e in audit:
        acts[e["action"]] = acts.get(e["action"], 0) + 1
    need = ("ingest", "ingest_rejected", "upload_pipeline", "review", "ask", "re_extract", "audit_denied")
    expect = {"viewer approve": 403, "viewer upload (POST /projects/{pid}/upload)": 403, "viewer audit": 403,
              "reviewer approve": 200, "admin upload": 200, "non-member documents": 403, "non-member ask": 403,
              "non-member upload": 403}
    ok = all(codes[k] == v for k, v in expect.items()) and all(acts.get(a_) for a_ in need)
    row(10, "Roles and controls", ok, ", ".join(f"{k} {v}" for k, v in codes.items()) + "; audit log actions "
        + ", ".join(f"{a_} {acts.get(a_, 0)}" for a_ in need), time.perf_counter() - t)


def row11_exports() -> None:
    t = time.perf_counter()
    c = http()
    res, ok = [], True

    def check(label, rr, fmt):
        nonlocal ok
        name = rr.headers.get("content-disposition", "").split("filename=")[-1].strip('"') or f"x.{fmt}"
        if rr.status_code != 200:
            ok = False
            res.append(f"{label} HTTP {rr.status_code}")
            return
        a1, _ = _check_file(rr.content, name, fmt, "review_status")
        a2 = any(_check_file(rr.content, name, fmt, k)[0] for k in ("citation", "page", "source"))
        ok &= a1 and a2
        res.append(f"{label} {'ok' if a1 and a2 else 'FAIL'}")

    for kind in ("inventory", "findings"):
        for fmt in ("xlsx", "csv", "json"):
            check(f"{kind}.{fmt}", c.get("/projects/body-comfort/export", headers=V, params={"kind": kind, "fmt": fmt}),
                  fmt)
    for label, status, content, name, fmt in CTX.get("diff_exports", [])[:3]:
        class _R:
            status_code, headers = status, {"content-disposition": f"filename={name}"}
        _R.content = content
        check(f"diff.{fmt}", _R, fmt)
    for pid, rid in CTX.get("reports", [])[:2]:
        for fmt in ("xlsx", "csv", "json"):
            check(f"report{rid}.{fmt}", c.get(f"/projects/{pid}/export/report", headers=V,
                                              params={"report_id": rid, "fmt": fmt}), fmt)
    row(11, "Exports", ok and bool(CTX.get("reports")), "; ".join(res) + " (each file opened: review_status column "
        "and citation/page/source column present)", time.perf_counter() - t)


def row12_ui() -> None:
    if not CHROME:
        row(12, "UI", False, "no Chromium for Playwright")
        return
    from playwright.sync_api import sync_playwright
    t = time.perf_counter()
    base = CTX["base"]
    info, ok, errs, cons = [], True, [], []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=str(CHROME[-1]))
        for w in (1280, 800):
            ctx = b.new_context(viewport={"width": w, "height": 900})
            ctx.add_cookies([{"name": "hld_user", "value": "admin", "url": base}])
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: cons.append(m.text) if m.type == "error" else None)
            for path in ("/?project=body-comfort", "/architecture?project=body-comfort", "/review?project=synthetic-ecu",
                         "/documents?project=synthetic-ecu"):
                pg.goto(base + path)
                pg.wait_for_load_state("networkidle")
                pg.wait_for_timeout(500)
                sc = pg.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                ok &= sc <= 0
                info.append(f"{path.split('?')[0]}@{w}: h-scroll {sc}px")
            ctx.close()
        ctx = b.new_context(viewport={"width": 1280, "height": 900})
        ctx.add_cookies([{"name": "hld_user", "value": "admin", "url": base}])
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(base + "/architecture?project=body-comfort")
        pg.wait_for_selector("#inventory table")
        pg.click("#tab-ports")
        tabs_ok = pg.get_attribute("#tab-ports", "aria-selected") == "true"
        pg.click("#inventory .linkish >> nth=0")
        pg.wait_for_selector("#side-panel:not([hidden])")
        pg.keyboard.press("Escape")
        esc_ok = pg.locator("#side-panel").is_hidden()
        pg.click("#settings-btn")
        pg.select_option("#project-select", "chassis")
        pg.wait_for_load_state("networkidle")
        switch_ok = "project=chassis" in pg.url and "Chassis" in pg.inner_text(".project-name")
        bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
        # sample questions: every listed one (except the deliberate off-topic one) is answered
        pg.goto(base + "/?project=synthetic-ecu")
        pg.wait_for_selector("#samples-box:not([hidden])", timeout=600000)
        last, stable = None, 0
        while stable < 20:
            cur = pg.inner_text("#samples")
            stable = stable + 1 if cur == last else 0
            last = cur
            pg.wait_for_timeout(1000)
        samples = [s for s in last.split("\n") if s.strip()]
        answered = 0
        for i, s in enumerate(samples):
            pg.click(f"#samples button >> nth={i}")
            pg.wait_for_function("() => !document.getElementById('ask-btn').disabled", timeout=600000)
            refused = pg.locator("#answer .answer.refused").count() > 0
            answered += (not refused) if "weather" not in s else refused
        ctx.close()
        decision_buttons = {}
        for user in ("viewer", "admin"):  # admin is the positive control: the same page shows the buttons
            ctx = b.new_context(viewport={"width": 1280, "height": 900})
            ctx.add_cookies([{"name": "hld_user", "value": user, "url": base}])
            pg = ctx.new_page()
            pg.goto(base + "/review?project=synthetic-ecu")
            pg.wait_for_selector("#findings .rule-group, #findings .state", timeout=60000)
            pg.wait_for_timeout(800)
            decision_buttons[user] = pg.locator("[data-decision]").count()
            ctx.close()
        approve_buttons = decision_buttons["viewer"]
        b.close()
    css = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
    online = [f.name for f in [*(ROOT / "app/templates").glob("*.html"), *(ROOT / "app/static/js").glob("*.js")]
              if re.search(r"https?://", f.read_text(encoding="utf-8"))]
    light = "prefers-color-scheme: dark" not in css and bg in ("rgb(255, 255, 255)",)
    ok &= tabs_ok and esc_ok and switch_ok and light and not online and approve_buttons == 0 \
        and decision_buttons["admin"] > 0 and not errs \
        and not cons and answered == len(samples)
    row(12, "UI", ok, f"4 pages x 2 widths load ({'; '.join(info)}); page errors {len(errs)}, console errors {len(cons)} "
        f"{cons[:2]}; Ports tab selects: {tabs_ok}; Esc closes the side panel: {esc_ok}; Settings switches project: "
        f"{switch_ok}; samples answerable {answered}/{len(samples)} ({' | '.join(samples)}); light theme only "
        f"(body {bg}, no dark media query): {light}; http(s):// in templates/JS: {online or 'none'}; approve/reject "
        f"buttons on Review: viewer {approve_buttons}, admin {decision_buttons['admin']}", time.perf_counter() - t)


def row13_models(gpu: GpuMonitor, off: int) -> None:
    from app import llm
    from app.config import settings
    qa_models = sorted({r_["model"] for r_ in CTX.get("qa_rows", []) if r_["model"] != "-"})
    up = CTX.get("syn_upload", {})
    try:
        bodies = [llm.OllamaProvider().payload(m, "x") for m in (settings.demo_model, settings.llm_extract_model)]
    except Exception:  # noqa: BLE001
        bodies = []
    body = {"think": all(b_.get("think") is False for b_ in bodies)} if bodies else {}
    tr = trace_entries(settings.trace_path)[off:]
    swaps = [f"{e.get('from_model') or 'none'} -> {e['model']} {e['latency_ms'] / 1000:.1f}s" for e in tr
             if e.get("purpose") == "model_swap"]
    v = gpu.snapshot() if hasattr(gpu, "snapshot") else {}
    ok = qa_models == [settings.demo_model] and up.get("extract_model") == settings.llm_extract_model \
        and body.get("think") is True
    row(13, "Models", ok, f"Q&A answers by {qa_models}; upload extraction by {up.get('extract_model')} (calls in "
        f"trace: {sum(1 for e in tr if str(e.get('purpose', '')).startswith('extract'))}, models "
        f"{sorted({e['model'] for e in tr if str(e.get('purpose', '')).startswith('extract')})}); think=false in the request body of both "
        f"models: {body.get('think')}; model swaps in data/traces.jsonl: {swaps[:6] or 'none'}; "
        f"`ollama ps`: {loaded()}; peak VRAM so far {max(gpu.samples) if gpu.samples else '-'} MiB")


def row14_offline() -> None:
    t = time.perf_counter()
    srv = Server(offline=True, extra={"LLM_CACHE_READ": "0"})  # a real LLM extraction, not a cache hit
    try:
        secs, h = srv.wait_ready()
        import httpx
        c = httpx.Client(base_url=srv.base, timeout=3600)
        a = c.post(f"/projects/{SCRATCH}/ask", headers=V, json={"question": "Who sends the MotorCommand signal and "
                                                                           "who receives it?"}).json()
        r = c.post(f"/projects/{SCRATCH}/extract", headers=A, json={"doc_id": CTX["syn_doc"], "use_llm": True})
        ex = r.json() if r.status_code == 200 else {}
        ok = h["warmup"]["status"] == "ready" and not (h.get("embedder") or {}).get("fallback") \
            and a.get("mode") == "generated" and r.status_code == 200
        row(14, "Offline", ok, f"HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1: ready in {secs:.1f}s, embedder fallback "
            f"{(h.get('embedder') or {}).get('fallback')}; ask {a.get('mode')} ({a.get('answer_type')}); LLM "
            f"re-extraction HTTP {r.status_code} {ex.get('extracted')} by {ex.get('extract_model')} in "
            f"{ms((ex.get('timings') or {}).get('extract_ms'))}", time.perf_counter() - t)
    finally:
        srv.stop()


def row15_demo_reset(cold_s: float | None, h: dict | None) -> None:
    t = time.perf_counter()
    target = WORK / "demo_reset_clean"
    shutil.rmtree(target, ignore_errors=True)
    env = {**BASE_ENV, "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run([sys.executable, "scripts/demo_reset.py", "--yes", "--data-dir", str(target),
                        "--db-path", str(target / "hld.db"), "--chroma-dir", str(target / "chroma"),
                        "--processed-dir", str(target / "processed"), "--raw-dir", str(ROOT / "data" / "raw")],
                       cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=7200)
    total = time.perf_counter() - t
    tail = [ln for ln in (p.stdout + p.stderr).splitlines() if ln.strip()][-6:]
    ok = p.returncode == 0 and cold_s is not None
    row(15, "Cold start and demo reset", ok, f"cold start (models unloaded -> /health ready, Q&A model warmed): "
        f"{cold_s:.1f} s (warm-up steps {(h or {}).get('warmup', {}).get('steps')}); scripts/demo_reset.py from a clean "
        f"state (new data dir, no LLM cache): exit {p.returncode}, total {total:.0f} s; last lines: {' / '.join(tail)}",
        total)


def row16_tests() -> None:
    t = time.perf_counter()
    res = {}
    for label, args in (("pytest -q", []), ("pytest -q -m llm", ["-m", "llm"])):
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", *args], cwd=ROOT,
                           capture_output=True, text=True, timeout=7200, env={**BASE_ENV, "PYTHONIOENCODING": "utf-8"})
        lines = p.stdout.strip().splitlines()
        skips = [ln for ln in lines if ln.startswith("SKIPPED")]
        res[label] = (p.returncode, lines[-1] if lines else "", skips)
    ok = all(v[0] == 0 for v in res.values())
    row(16, "Tests", ok, "; ".join(f"{k}: {v[1]}" + (f" (skips: {' | '.join(v[2])[:300]})" if v[2] else "")
                                   for k, v in res.items()), time.perf_counter() - t)


def row17_gold() -> None:
    from app import controls
    from app.config import settings
    from app.extract import get_inventory
    from eval import run_eval as E
    t = time.perf_counter()
    qs = json.loads((EVAL_DIR / "gold_questions.json").read_text(encoding="utf-8"))["questions"]
    os.environ["LLM_CACHE_READ"] = "0"
    try:
        conn = controls.get_conn(E.copy_db("acceptance_gold_qa"))
        qa_ = E.run_qa(conn, settings.demo_model, qs, W / "gold_traces.jsonl")["summary"]
        conn.close()
        keys = ("answer_correctness", "citation_accuracy", "section_accuracy", "refusal_accuracy_unanswerable",
                "false_refusal_rate_answerable")
        note("17 gold Q&A: " + json.dumps({k: qa_.get(k) for k in keys}))
        gold = E.load_gold_csv(EVAL_DIR / "real_gold.csv", verified_only=True)
        real = {}
        for label, model in (("deterministic", None), (settings.llm_extract_model, settings.llm_extract_model)):
            if model:
                unload_all()  # as in the recorded runs: the extraction model starts alone on the GPU
            conn = controls.get_conn(E.copy_db(f"acceptance_real_gold_{label.replace(':', '_').replace('.', '_')}"))
            E.extract_all(conn, model, model is not None, set(E.REAL_DOCS))
            inv = {d: get_inventory(r_["project_id"], d, None, conn) for d in E.REAL_DOCS
                   for r_ in conn.execute("SELECT project_id FROM documents WHERE doc_id=?", (d,))}
            conn.close()
            sc = E.score_real_gold(gold, inv)
            real[label] = {"mean": E._mean_f1(sc), "by_type": {k: round(v["f1_page_scoped"], 3) for k, v in sc.items()
                                                              if v.get("f1_page_scoped") is not None}}
            note(f"17 real gold ({label}): mean F1 {real[label]['mean']:.3f} {real[label]['by_type']}")
    finally:
        os.environ.pop("LLM_CACHE_READ", None)
    prev = json.loads((EVAL_DIR / "results.json").read_text(encoding="utf-8"))
    rec = {"deterministic": prev["deterministic"]["A_mean_f1"],
           settings.llm_extract_model: prev["models"][settings.llm_extract_model]["run1"]["A_mean_f1"]}
    ok = qa_.get("answer_correctness", 0) >= 0.95 and qa_.get("refusal_accuracy_unanswerable", 0) >= 1.0 \
        and qa_.get("citation_accuracy", 0) >= 0.95 and all(real[k]["mean"] >= rec[k] - 1e-9 for k in real)
    CTX["gold"] = {"qa": qa_, "real_gold": real, "recorded": rec}
    row(17, "Gold regression (run once, not tuned)", ok,
        f"25 gold questions ({settings.demo_model}, cache reads off): correct {pct(qa_.get('answer_correctness'))}, "
        f"citation accuracy {pct(qa_.get('citation_accuracy'))}, section accuracy {pct(qa_.get('section_accuracy'))}, "
        f"refusal {pct(qa_.get('refusal_accuracy_unanswerable'))} (last recorded 95% / 95% / 85% / 100%); 51 real-gold "
        f"rows, mean page-scoped F1: " + "; ".join(f"{k} {real[k]['mean']:.3f} vs {rec[k]:.3f} recorded "
                                                   f"{real[k]['by_type']}" for k in real), time.perf_counter() - t)


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", nargs="*", type=int)
    ap.add_argument("--skip-slow", action="store_true")
    a = ap.parse_args()
    want = set(a.rows or range(1, 18)) - ({16, 17} if a.skip_slow else set())
    from app.config import settings
    log(f"\n## Acceptance run {time.strftime('%Y-%m-%d %H:%M')} (eval/acceptance_check.py; real API on a scratch copy "
        f"{W}; Q&A {settings.demo_model}, extraction {settings.llm_extract_model})")
    off = len(trace_entries(settings.trace_path))
    gpu = GpuMonitor(every=2.0)
    gpu.start()
    cold_s, h = None, None
    if want & set(range(1, 16)):
        prepare()
        unload_all()
        srv = Server()
        try:
            cold_s, h = srv.wait_ready()
            CTX["base"] = srv.base
            steps = [(1, lambda: row1_environment(h)), (2, row2_ingestion), (3, row3_ocr), (4, row4_extraction),
                     (5, row5_checks), (6, row6_compare), (7, row7_graph), (8, row8_reports), (9, row9_qa),
                     (10, row10_roles), (11, row11_exports), (12, row12_ui), (13, lambda: row13_models(gpu, off))]
            for n, fn in steps:
                if n in want:
                    try:
                        fn()
                    except Exception as e:  # noqa: BLE001 - a crashing row is a FAIL with the error as evidence
                        row(n, f"row {n}", False, f"error: {type(e).__name__}: {e}")
        finally:
            srv.stop()
        for n, fn in ((14, row14_offline), (15, lambda: row15_demo_reset(cold_s, h))):
            if n in want and not (n == 15 and a.skip_slow):
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    row(n, f"row {n}", False, f"error: {type(e).__name__}: {e}")
    for n, fn in ((16, row16_tests), (17, row17_gold)):
        if n in want:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                row(n, f"row {n}", False, f"error: {type(e).__name__}: {e}")
    vram = gpu.stop()
    log(f"     peak VRAM during the run: {vram.get('peak_mib')} of {vram.get('total_mib')} MiB")
    tbl = table(["#", "component", "result", "evidence"],
                [[str(r_["row"]), r_["name"], "PASS" if r_["ok"] else "FAIL", r_["evidence"].replace("|", "/")[:600]]
                 for r_ in sorted(ROWS.values(), key=lambda x: x["row"])])
    log("\n" + tbl)
    R_ = json.loads((EVAL_DIR / "results.json").read_text(encoding="utf-8"))
    R_.setdefault("acceptance", {}).update({"generated": time.strftime("%Y-%m-%d %H:%M"), "vram": vram,
                                            **{str(k): v for k, v in ROWS.items()}, "qa_rows": CTX.get("qa_rows")})
    (EVAL_DIR / "results.json").write_text(json.dumps(R_, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    all_rows = {int(k): v for k, v in R_["acceptance"].items() if k.isdigit()}
    md = (EVAL_DIR / "results.md").read_text(encoding="utf-8").split("\n## Acceptance run", 1)[0].rstrip()
    full = table(["#", "component", "result", "evidence"],
                 [[str(k), v["name"], "PASS" if v["ok"] else "FAIL", v["evidence"].replace("|", "/")[:600]]
                  for k, v in sorted(all_rows.items())])
    (EVAL_DIR / "results.md").write_text(md + f"\n\n## Acceptance run ({R_['acceptance']['generated']})\n\n" + full
                                         + "\n", encoding="utf-8")
    return 0 if all(r_["ok"] for r_ in ROWS.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
