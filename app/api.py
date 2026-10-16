"""FastAPI service. Pilot auth: the X-User header names a seeded user. Every project endpoint enforces
project membership and a minimum role (viewer < reviewer < admin).

Paths (db / chroma / processed) come from the ``get_paths`` dependency; tests override it with
``app.dependency_overrides[api.get_paths] = lambda: api.Paths(...)``.
"""
from __future__ import annotations

import mimetypes
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from app import auth, checks, compare as compare_mod, controls, export, extract, graph as graph_mod, llm, qa, retrieve, summarize
from app import ingest as ingest_mod
from app.config import settings
from app.ingest import IngestRejected, ingest_document, load_processed

WARMUP: dict = {"status": "ready", "steps": {}, "reason": ""}  # status: warming | ready | degraded


def _warmup() -> None:
    """Load the embedder and send a 1-token prompt to the demo (Q&A) model only, kept loaded for DEMO_KEEP_ALIVE;
    the extraction model (LLM_EXTRACT_MODEL) loads on first use. Never raises."""
    errs: list[str] = []
    try:
        retrieve.get_embedder()
        WARMUP["steps"]["embedder"] = "ok"
    except Exception as e:  # noqa: BLE001
        WARMUP["steps"]["embedder"] = "failed"
        errs.append(f"embedder: {str(e)[:120]}")
    try:
        if settings.llm_provider == "ollama":
            WARMUP["steps"]["llm_load_ms"] = llm.OllamaProvider(timeout=300).warm(settings.demo_model)["load_ms"]
            WARMUP["steps"]["llm"] = "ok"
        else:
            WARMUP["steps"]["llm"] = "skipped"
    except Exception as e:  # noqa: BLE001
        WARMUP["steps"]["llm"] = "failed"
        errs.append(f"llm: {str(e)[:120]}")
    WARMUP["reason"] = "; ".join(errs)
    WARMUP["status"] = "degraded" if errs else "ready"


@asynccontextmanager
async def _lifespan(_: FastAPI):
    if os.environ.get("WARMUP", "1") != "0" and os.environ.get("PYTEST_CURRENT_TEST") is None:
        WARMUP.update(status="warming", steps={}, reason="")
        threading.Thread(target=_warmup, daemon=True, name="warmup").start()
    yield


app = FastAPI(title="HLD Assistant API", version="0.6", lifespan=_lifespan)
mimetypes.add_type("font/woff2", ".woff2")  # not in every OS registry (Windows)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


def _demo(model: str | None) -> str | None:
    """Per-request model, else DEMO_MODEL (when allowed) for interactive requests."""
    if model is not None:
        return model
    return settings.demo_model if settings.demo_model in settings.allowed_models else None


@dataclass
class Paths:
    db_path: Path
    chroma_dir: Path
    processed_dir: Path


def get_paths() -> Paths:
    return Paths(settings.db_path, settings.chroma_dir, settings.processed_dir)


def get_db(paths: Paths = Depends(get_paths)) -> Iterator[sqlite3.Connection]:
    c = controls.get_conn(paths.db_path, same_thread=False)  # sync deps/endpoints may run on different threads
    try:
        yield c
    finally:
        c.close()


def get_user(request: Request, x_user: str | None = Header(default=None)) -> str:
    return auth.identify(request, x_user)


class LoginBody(BaseModel):
    token: str


@app.post("/login")
def login(body: LoginBody, request: Request, c: sqlite3.Connection = Depends(get_db)) -> Response:
    if not auth.enabled():
        raise HTTPException(400, "Access tokens are not configured; this is a local demo.")
    # Resolve the supplied token using the same validation path as bearer clients.
    scope = {**request.scope, "headers": [(b"authorization", ("Bearer " + body.token).encode())]}
    user = auth.identify(Request(scope))
    if controls.get_role(user, c) is None:
        raise HTTPException(403, "This account has not been provisioned.")
    response = JSONResponse({"ok": True})
    response.set_cookie(auth.COOKIE, body.token, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=28800)
    return response


@app.post("/logout")
def logout() -> Response:
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE)
    return response


def get_provider_dep() -> llm.Provider:
    return llm.get_provider()


@app.exception_handler(controls.PermissionDenied)
async def _denied(_: Request, e: controls.PermissionDenied) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=403)


@app.exception_handler(IngestRejected)
async def _rejected(_: Request, e: IngestRejected) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(ValueError)
async def _not_found(_: Request, e: ValueError) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=404)


@app.exception_handler(KeyError)
async def _key(_: Request, e: KeyError) -> JSONResponse:
    return JSONResponse({"detail": f"not found: {e}"}, status_code=404)


def _need(c: sqlite3.Connection, user: str, pid: str, role: str) -> None:
    if not c.execute("SELECT 1 FROM projects WHERE project_id=?", (pid,)).fetchone():
        # do not leak project existence to non-members: unknown user/membership gives 403 first
        controls.require_role(user, pid, role, c)
        raise ValueError(f"unknown project {pid!r}")
    controls.require_role(user, pid, role, c)


def _doc(c: sqlite3.Connection, pid: str, doc_id: str | None) -> sqlite3.Row:
    q, args = "SELECT * FROM documents WHERE project_id=?", [pid]
    if doc_id:
        q += " AND doc_id=?"
        args.append(doc_id)
    row = c.execute(q + " ORDER BY uploaded_at, doc_id", args).fetchone()
    if row is None:
        raise ValueError(f"unknown document {doc_id!r}" if doc_id else f"project {pid!r} has no documents")
    return row


@app.get("/health")
def health() -> dict:
    ollama = llm.ollama_status()
    embedding = retrieve.embedder_status()
    available = ollama.get("models", [])
    missing = [model for model in dict.fromkeys((settings.demo_model, settings.llm_extract_model)) if model not in available]
    ready = (settings.llm_provider == "ollama" and ollama.get("reachable", False)
             and settings.demo_model in available and not embedding["fallback"] and WARMUP["status"] != "warming")
    return {"status": "ok", "llm_mode": llm.get_provider().name, "ollama": ollama,
            "allowed_models": list(settings.allowed_models), "default_model": settings.ollama_model,
            "demo_model": settings.demo_model, "warmup": {"status": WARMUP["status"], "reason": WARMUP["reason"],
                                                       "steps": dict(WARMUP["steps"])},
            "embedder": embedding, "auth_mode": "token" if auth.enabled() else "local_demo",
            "qa_ready": bool(ready), "missing_models": missing}


@app.get("/me")
def me(user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db)) -> dict:
    """Who am I: global role (admin gates /audit). Per-project roles are in /projects."""
    role = controls.get_role(user, c)
    if role is None:
        raise controls.PermissionDenied(f"unknown user {user!r}")
    return {"user": user, "role": role, "is_admin": role == "admin"}


@app.get("/projects")
def list_projects(user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    if controls.get_role(user, c) is None:
        raise controls.PermissionDenied(f"unknown user {user!r}")
    rows = c.execute("SELECT p.project_id, p.name FROM projects p JOIN project_members m "
                     "ON m.project_id=p.project_id WHERE m.username=? ORDER BY p.project_id", (user,)).fetchall()
    has_docs = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='documents'").fetchone()
    counts = dict(c.execute("SELECT project_id, COUNT(*) FROM documents GROUP BY project_id").fetchall())         if has_docs else {}
    return [{"project_id": r["project_id"], "name": r["name"], "documents": counts.get(r["project_id"], 0),
             "role": controls.get_member_role(user, r["project_id"], c)} for r in rows]


def rewarm_qa_model(provider) -> dict | None:
    """After LLM extraction (LLM_EXTRACT_MODEL) the Q&A model was likely evicted from the 4 GB GPU: reload it with a
    1-token call so the next question is not slow. /health shows 'warming' meanwhile. None when not applicable."""
    if not isinstance(provider, llm.OllamaProvider) or settings.llm_extract_model == settings.demo_model:
        return None
    WARMUP.update(status="warming")
    WARMUP["steps"]["llm"] = "rewarming"
    try:
        out = provider.warm(settings.demo_model, purpose="rewarm")
        WARMUP["steps"]["llm"] = "ok"
        WARMUP["status"] = "ready" if not WARMUP.get("reason") else "degraded"
        return out
    except Exception as e:  # noqa: BLE001 - the upload itself succeeded; the next question just loads the model
        WARMUP["steps"]["llm"] = "rewarm failed"
        WARMUP["status"] = "ready" if not WARMUP.get("reason") else "degraded"
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}


def _pipeline(c, pid: str, doc_id: str, version: str | None, paths: Paths, use_llm: bool, model: str | None,
              provider, pdf_path=None) -> dict:
    """Extraction (diagram + convention + heuristics; LLM prose layer on LLM_EXTRACT_MODEL when use_llm) followed by
    the deterministic checks and, after LLM extraction, a re-warm of the Q&A model. Returns per-step timings."""
    t0 = time.perf_counter()
    counts = extract.extract_document(pid, doc_id, conn=c, processed_dir=paths.processed_dir, use_llm=use_llm,
                                      pdf_path=pdf_path, provider=provider if use_llm else None, model=model)
    t1 = time.perf_counter()
    found = checks.run_checks(pid, doc_id, version, conn=c, processed_dir=paths.processed_dir)
    t2 = time.perf_counter()
    warm = rewarm_qa_model(provider) if use_llm else None
    timings = {"extract_ms": int((t1 - t0) * 1000), "checks_ms": int((t2 - t1) * 1000),
               "rewarm_ms": int((time.perf_counter() - t2) * 1000) if warm else 0}
    return {"extracted": counts, "findings": len(found), "timings": timings, "rewarm": warm,
            "extract_model": (model or settings.llm_extract_model) if use_llm else None,
            "findings_by_rule": {r: sum(1 for f in found if f.rule_id == r) for r in sorted({f.rule_id for f in found})}}


@app.post("/projects/{pid}/upload")
def upload(pid: str, file: UploadFile = File(...), doc_type: str = Form(...), version: str = Form(...),
           source_status: str = Form(...), use_llm: bool = Form(True), model: str | None = Form(None),
           user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db),
           paths: Paths = Depends(get_paths), provider: llm.Provider = Depends(get_provider_dep)) -> dict:
    """Ingest + extract (LLM prose layer on LLM_EXTRACT_MODEL unless use_llm=false) + checks + re-warm of the Q&A
    model. The response carries per-step timings."""
    _need(c, user, pid, "admin")
    _check_model(model)
    raw_name = file.filename or "upload.pdf"
    bad = ingest_mod._name_problem(raw_name)
    if bad:  # before the name touches the file system
        ingest_mod._reject(c, user, pid, raw_name, bad)
    with tempfile.TemporaryDirectory() as td:
        name = Path(raw_name).name
        dest = Path(td) / name
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        t0 = time.perf_counter()
        doc_id = ingest_document(dest, pid, doc_type, version, source_status, user, conn=c,
                                 processed_dir=paths.processed_dir, chroma_dir=paths.chroma_dir,
                                 original_name=file.filename or "upload.pdf")
        ingest_ms = int((time.perf_counter() - t0) * 1000)
        out = _pipeline(c, pid, doc_id, version, paths, use_llm, model, provider, pdf_path=dest)
        out["timings"] = {"ingest_ms": ingest_ms, **out["timings"]}
        keep = _pdf_store(paths, pid) / f"{doc_id}.pdf"  # kept so evidence pages can be rendered later
        keep.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dest, keep)
    controls.log_action(user, "upload_pipeline", pid, {"doc_id": doc_id, **{k: out[k] for k in ("extracted", "findings")}}, c)
    return {"doc_id": doc_id, "version": version, "use_llm": use_llm, **out}


class ExtractBody(BaseModel):
    doc_id: str
    use_llm: bool = False
    model: str | None = None


@app.post("/projects/{pid}/extract")
def re_extract(pid: str, body: ExtractBody, user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db),
               paths: Paths = Depends(get_paths), provider: llm.Provider = Depends(get_provider_dep)) -> dict:
    """Re-run extraction + checks on an ingested document. Review decisions on matching entities are preserved."""
    _need(c, user, pid, "admin")
    _check_model(body.model)
    d = _doc(c, pid, body.doc_id)
    out = _pipeline(c, pid, d["doc_id"], d["version"], paths, body.use_llm, body.model, provider,
                    pdf_path=_find_pdf(c, pid, d["doc_id"], paths))
    controls.log_action(user, "re_extract", pid, {"doc_id": d["doc_id"], **{k: out[k] for k in ("extracted", "findings")}}, c)
    return {"doc_id": d["doc_id"], "version": d["version"], "use_llm": body.use_llm, **out}


def _n_pages(pid: str, doc_id: str, paths: Paths) -> int | None:
    try:
        return load_processed(pid, doc_id, paths.processed_dir).get("n_pages")
    except (OSError, ValueError):
        return None


@app.get("/projects/{pid}/documents")
def documents(pid: str, user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db),
              paths: Paths = Depends(get_paths)) -> list[dict]:
    _need(c, user, pid, "viewer")
    return [{**{k: r[k] for k in ("doc_id", "title", "doc_type", "version", "source_status", "uploaded_by",
                                  "uploaded_at")}, "pages": _n_pages(pid, r["doc_id"], paths)}
            for r in c.execute("SELECT * FROM documents WHERE project_id=? ORDER BY doc_id", (pid,))]


class AskBody(BaseModel):
    question: str
    k: int = 5
    model: str | None = None  # one of settings.allowed_models; anything else is rejected (422)
    check: bool = False  # sample-question check: answered but not stored (audited as "ask_check")

    @field_validator("model")
    @classmethod
    def _model_allowed(cls, v: str | None) -> str | None:
        if v is not None and v not in settings.allowed_models:
            raise ValueError(f"model must be one of {list(settings.allowed_models)}")
        return v


@app.post("/projects/{pid}/ask")
def ask(pid: str, body: AskBody, user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db),
        paths: Paths = Depends(get_paths), provider: llm.Provider = Depends(get_provider_dep)) -> dict:
    _need(c, user, pid, "viewer")
    return qa.ask(pid, body.question, user, k=body.k, conn=c, chroma_dir=paths.chroma_dir,
                  provider=provider, model=_demo(body.model), store=not body.check).model_dump()


@app.get("/projects/{pid}/inventory")
def inventory(pid: str, doc_id: str | None = None, version: str | None = None,
              user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db)) -> dict:
    _need(c, user, pid, "viewer")
    return extract.get_inventory(pid, doc_id, version, c)


@app.get("/projects/{pid}/coverage")
def coverage(pid: str, doc_id: str, user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db),
             paths: Paths = Depends(get_paths)) -> dict:
    _need(c, user, pid, "viewer")
    _doc(c, pid, doc_id)
    doc = load_processed(pid, doc_id, paths.processed_dir)
    inv = extract.get_inventory(pid, doc_id, conn=c)
    counts = {t: {"total": len(rows), "uncertain": sum(r["confidence"] != "High" for r in rows),
                  "without_evidence": sum(not any(s.get("evidence") or s.get("bbox") for s in r["sources"]) for r in rows),
                  "not_rediscovered": sum(not r["extraction_present"] for r in rows),
                  "rejected": sum(r["review_status"] == "rejected" for r in rows)} for t, rows in inv.items()}
    warnings = ["Extraction is an inventory of what the system found; completeness has not been established."]
    for table, rows in inv.items():
        if not rows:
            warnings.append(f"No {table} were extracted. Check the source before concluding they are absent.")
    if any(x.get("status") != "ok" for x in doc.get("ocr_pages", [])):
        warnings.append("Some scanned pages could not be read completely; inspect the source pages.")
    return {"doc_id": doc_id, "counts": counts, "warnings": warnings,
            "completeness": "not_measured", "ocr_pages": doc.get("ocr_pages", [])}


def _pdf_store(paths: Paths, pid: str) -> Path:
    return paths.processed_dir / pid / "pdfs"


@app.get("/projects/{pid}/graph")
def graph(pid: str, doc_id: str | None = None, component: str | None = None, depth: int = 1,
          user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db),
          paths: Paths = Depends(get_paths)) -> dict:
    """Dependency graph (pyvis html + json). With `component`, only its neighbourhood within `depth` hops
    (1..4, edges followed in both directions) is returned."""
    import json
    import re
    import networkx as nx
    _need(c, user, pid, "viewer")
    d = _doc(c, pid, doc_id)
    g = graph_mod.build_graph(pid, d["doc_id"], d["version"], c)
    total = (g.number_of_nodes(), g.number_of_edges())
    comps = sorted(a["name"] for _, a in g.nodes(data=True) if a["type"] == "component")
    if component:
        root = graph_mod._comp_id(g, component)  # KeyError -> 404
        keep = nx.single_source_shortest_path_length(g.to_undirected(as_view=True), root, cutoff=max(1, min(depth, 4)))
        g = g.subgraph(keep).copy()
    out = graph_mod.graph_dir(pid, paths.processed_dir)
    stem = d["doc_id"] if not component else f"{d['doc_id']}__{re.sub(r'[^A-Za-z0-9_-]', '_', component)}_{depth}"
    jpath = graph_mod.export_json(g, out / f"{stem}.json")
    hpath = graph_mod.export_pyvis(g, out / f"{stem}.html")
    evidence = []
    for n, a in g.nodes(data=True):
        m = re.search(r"p\.(\d+)", a.get("citation") or "")
        evidence.append({"id": n, "type": a["type"], "name": a["name"], "citation": a.get("citation"),
                         "page": int(m.group(1)) if m else None})
    return {"doc_id": d["doc_id"], "version": d["version"], "component": component, "depth": depth,
            "nodes": g.number_of_nodes(), "edges": g.number_of_edges(), "total_nodes": total[0],
            "total_edges": total[1], "components": comps, "evidence": evidence,
            "json": json.loads(jpath.read_text(encoding="utf-8")),
            "html_path": str(hpath), "html": hpath.read_text(encoding="utf-8")}


def _find_pdf(c, pid: str, doc_id: str, paths: Paths) -> Path | None:
    from app import diagram
    own = _pdf_store(paths, pid) / f"{doc_id}.pdf"
    if own.is_file():
        return own
    try:
        doc = load_processed(pid, doc_id, paths.processed_dir)
    except Exception:
        return None
    return diagram.find_pdf(doc)


@app.get("/projects/{pid}/documents/{doc_id}/pages/{n}.png")
def page_png(pid: str, doc_id: str, n: int, highlight: str | None = None, zoom: float = 1.4,
             user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db),
             paths: Paths = Depends(get_paths)) -> Response:
    """Render page n (1-based) of an ingested document as PNG, optionally highlighting a text. Members only."""
    import pymupdf
    _need(c, user, pid, "viewer")
    d = _doc(c, pid, doc_id)
    if d["doc_id"] != doc_id:
        raise ValueError(f"unknown document {doc_id!r}")
    pdf = _find_pdf(c, pid, doc_id, paths)
    if pdf is None:
        raise ValueError(f"source PDF for {doc_id!r} is not available")
    with pymupdf.open(pdf) as pdoc:
        if not 1 <= n <= pdoc.page_count:
            raise ValueError(f"page {n} out of range 1..{pdoc.page_count}")
        page = pdoc[n - 1]
        if highlight:
            for r in page.search_for(highlight[:60])[:20]:
                page.add_highlight_annot(r)
        png = page.get_pixmap(matrix=pymupdf.Matrix(max(0.5, min(zoom, 3.0)), max(0.5, min(zoom, 3.0)))).tobytes("png")
    return Response(png, media_type="image/png")


EVAL_DIR = settings.project_root / "eval"
_CHART_RE = r"^[A-Za-z0-9_-]+\.png$"


@app.get("/eval/results")
def eval_results(user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db)) -> dict:
    """Evaluation report (markdown), raw metrics and the list of available charts. Any known user."""
    import json
    if controls.get_role(user, c) is None:
        raise controls.PermissionDenied(f"unknown user {user!r}")
    md, js = EVAL_DIR / "results.md", EVAL_DIR / "results.json"
    if not md.is_file():
        raise ValueError("no evaluation results yet (run eval/run_eval.py)")
    charts = sorted(p.name for p in (EVAL_DIR / "charts").glob("*.png")) if (EVAL_DIR / "charts").is_dir() else []
    return {"markdown": md.read_text(encoding="utf-8"),
            "results": json.loads(js.read_text(encoding="utf-8")) if js.is_file() else None, "charts": charts}


@app.get("/eval/charts/{name}")
def eval_chart(name: str, user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db)) -> FileResponse:
    import re
    if controls.get_role(user, c) is None:
        raise controls.PermissionDenied(f"unknown user {user!r}")
    p = EVAL_DIR / "charts" / name
    if not re.match(_CHART_RE, name) or not p.is_file():
        raise ValueError(f"unknown chart {name!r}")
    return FileResponse(p, media_type="image/png")


@app.get("/projects/{pid}/findings")
def findings(pid: str, doc_id: str | None = None, rule_id: str | None = None, user: str = Depends(get_user),
             c: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    _need(c, user, pid, "viewer")
    return [f.model_dump() for f in checks.get_findings(pid, doc_id, None, rule_id, c)]


class RunChecksBody(BaseModel):
    doc_id: str | None = None


@app.post("/projects/{pid}/run-checks")
def run_checks(pid: str, body: RunChecksBody | None = None, user: str = Depends(get_user),
               c: sqlite3.Connection = Depends(get_db), paths: Paths = Depends(get_paths)) -> dict:
    _need(c, user, pid, "reviewer")
    doc_id = body.doc_id if body else None
    docs = [_doc(c, pid, doc_id)] if doc_id else c.execute(
        "SELECT * FROM documents WHERE project_id=? ORDER BY doc_id", (pid,)).fetchall()
    counts = {}
    for d in docs:
        counts[d["doc_id"]] = len(checks.run_checks(pid, d["doc_id"], d["version"], conn=c,
                                                    processed_dir=paths.processed_dir))
    controls.log_action(user, "run_checks", pid, counts, c)
    return {"findings": counts}


def _check_model(model: str | None) -> str | None:
    if model is not None and model not in settings.allowed_models:
        raise HTTPException(422, f"model must be one of {list(settings.allowed_models)}")
    return model


@app.get("/projects/{pid}/reports/component")
def report_component(pid: str, component: str, doc_id: str | None = None, model: str | None = None,
                     user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db),
                     paths: Paths = Depends(get_paths), provider: llm.Provider = Depends(get_provider_dep)) -> dict:
    _need(c, user, pid, "viewer")
    d = _doc(c, pid, doc_id)
    return summarize.component_report(pid, d["doc_id"], d["version"], component, conn=c, provider=provider,
                                      model=_demo(_check_model(model)), chroma_dir=paths.chroma_dir)


@app.get("/projects/{pid}/reports/document")
def report_document(pid: str, doc_id: str | None = None, model: str | None = None,
                    user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db),
                    paths: Paths = Depends(get_paths), provider: llm.Provider = Depends(get_provider_dep)) -> dict:
    _need(c, user, pid, "viewer")
    d = _doc(c, pid, doc_id)
    return summarize.document_summary(pid, d["doc_id"], d["version"], conn=c, processed_dir=paths.processed_dir,
                                      provider=provider, model=_demo(_check_model(model)))


@app.get("/projects/{pid}/reports/flow")
def report_flow(pid: str, doc_id: str | None = None, user: str = Depends(get_user),
                c: sqlite3.Connection = Depends(get_db), paths: Paths = Depends(get_paths)) -> dict:
    _need(c, user, pid, "viewer")
    d = _doc(c, pid, doc_id)
    return summarize.functional_flow_summary(pid, d["doc_id"], d["version"], conn=c,
                                             processed_dir=paths.processed_dir)


class CompareBody(BaseModel):
    doc_a: str
    doc_b: str
    version_a: str | None = None
    version_b: str | None = None


@app.post("/projects/{pid}/compare")
def compare(pid: str, body: CompareBody, user: str = Depends(get_user),
            c: sqlite3.Connection = Depends(get_db), paths: Paths = Depends(get_paths)) -> dict:
    _need(c, user, pid, "viewer")
    a, b = _doc(c, pid, body.doc_a), _doc(c, pid, body.doc_b)
    diff = compare_mod.compare(pid, a["doc_id"], body.version_a or a["version"], b["doc_id"],
                               body.version_b or b["version"], conn=c, processed_dir=paths.processed_dir)
    return diff.model_dump()


class ReviewBody(BaseModel):
    item_type: Literal["component", "port", "interface", "signal", "finding", "report", "answer"]
    item_id: int
    decision: Literal["approved", "rejected", "corrected"]
    comment: str = ""


@app.post("/projects/{pid}/review")
def review(pid: str, body: ReviewBody, user: str = Depends(get_user),
           c: sqlite3.Connection = Depends(get_db), paths: Paths = Depends(get_paths)) -> dict:
    _need(c, user, pid, "reviewer")
    table = controls.REVIEW_TABLES[body.item_type]
    exists = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists or not c.execute(f"SELECT 1 FROM {table} WHERE id=? AND project_id=?",
                                   (body.item_id, pid)).fetchone():
        raise ValueError(f"unknown {body.item_type} {body.item_id} in project {pid!r}")
    controls.review(body.item_type, body.item_id, body.decision, body.comment, user, project_id=pid, conn=c)
    if body.item_type in extract.ENTITY_TABLES:
        entity = c.execute(f"SELECT doc_id, version FROM {table} WHERE id=?", (body.item_id,)).fetchone()
        checks.run_checks(pid, entity["doc_id"], entity["version"], c, paths.processed_dir)
    return {"ok": True, "item_type": body.item_type, "item_id": body.item_id, "review_status": body.decision}


class CorrectionBody(BaseModel):
    item_type: Literal["component", "port", "interface", "signal"]
    item_id: int
    changes: dict
    comment: str


@app.post("/projects/{pid}/corrections")
def correct(pid: str, body: CorrectionBody, user: str = Depends(get_user),
            c: sqlite3.Connection = Depends(get_db), paths: Paths = Depends(get_paths)) -> dict:
    _need(c, user, pid, "reviewer")
    if not body.comment.strip():
        raise HTTPException(422, "Explain the correction before saving.")
    try:
        result = extract.correct_entity(pid, body.item_type, body.item_id, body.changes, body.comment, user, c)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    findings = checks.run_checks(pid, result["doc_id"], result["version"], c, paths.processed_dir)
    return {"ok": True, "review_status": "corrected", "findings": len(findings), **result}


def _send(files: list[Path], out_dir: Path, stem: str, fmt: str) -> FileResponse:
    if len(files) == 1:
        return FileResponse(files[0], filename=files[0].name)
    zpath = out_dir / f"{stem}_{fmt}.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for f in files:
            z.write(f, f.name)
    return FileResponse(zpath, filename=zpath.name)


@app.get("/projects/{pid}/export")
def export_file(pid: str, kind: Literal["inventory", "findings"] = "inventory",
                fmt: Literal["csv", "xlsx", "json"] = "xlsx", doc_id: str | None = None,
                user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db),
                paths: Paths = Depends(get_paths)) -> FileResponse:
    _need(c, user, pid, "viewer")
    out_dir = paths.processed_dir / pid / "exports"
    fn = export.export_inventory if kind == "inventory" else export.export_findings
    files = fn(pid, fmt, out_dir=out_dir, doc_id=doc_id, conn=c)
    controls.log_action(user, "export", pid, {"kind": kind, "fmt": fmt}, c)
    return _send(files, out_dir, f"{kind}_{doc_id or 'all'}", fmt)


@app.get("/projects/{pid}/export/diff")
def export_diff_file(pid: str, doc_a: str, doc_b: str, fmt: Literal["csv", "xlsx", "json"] = "xlsx",
                     version_a: str | None = None, version_b: str | None = None, user: str = Depends(get_user),
                     c: sqlite3.Connection = Depends(get_db), paths: Paths = Depends(get_paths)) -> FileResponse:
    """Release diff (added/removed/changed/possible renames + change history) with review_status and citations."""
    _need(c, user, pid, "viewer")
    a, b = _doc(c, pid, doc_a), _doc(c, pid, doc_b)
    diff = compare_mod.compare(pid, a["doc_id"], version_a or a["version"], b["doc_id"], version_b or b["version"],
                               conn=c, processed_dir=paths.processed_dir)
    out_dir = paths.processed_dir / pid / "exports"
    files = export.export_diff(diff, fmt, out_dir=out_dir)
    controls.log_action(user, "export", pid, {"kind": "diff", "fmt": fmt, "a": doc_a, "b": doc_b}, c)
    return _send(files, out_dir, f"diff_{doc_a}__{doc_b}", fmt)


@app.get("/projects/{pid}/reports")
def list_reports(pid: str, kind: str | None = None, user: str = Depends(get_user),
                 c: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    """Stored reports (newest first): id, doc_id, version, kind, target, confidence, review_status, created_at."""
    _need(c, user, pid, "viewer")
    summarize.ensure_schema(c)
    q, args = ("SELECT id, doc_id, version, kind, target, confidence, review_status, created_at FROM reports "
         "WHERE project_id=?"), [pid]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    return [dict(r) for r in c.execute(q + " ORDER BY id DESC", args)]


@app.get("/projects/{pid}/export/report")
def export_report_file(pid: str, report_id: int, fmt: Literal["csv", "xlsx", "json"] = "xlsx",
                       user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db),
                       paths: Paths = Depends(get_paths)) -> FileResponse:
    """Export a stored report (component / document summary / flow) with review_status and citations."""
    import json
    _need(c, user, pid, "viewer")
    summarize.ensure_schema(c)
    row = c.execute("SELECT * FROM reports WHERE id=? AND project_id=?", (report_id, pid)).fetchone()
    if row is None:
        raise ValueError(f"unknown report {report_id} in project {pid!r}")
    report = {**json.loads(row["content_json"]), "id": row["id"], "review_status": row["review_status"]}
    out_dir = paths.processed_dir / pid / "exports"
    files = export.export_stored_report(report, fmt, out_dir)
    controls.log_action(user, "export", pid, {"kind": "report", "fmt": fmt, "report_id": report_id}, c)
    return _send(files, out_dir, f"report_{report_id}", fmt)


@app.get("/audit")
def audit(limit: int = 200, user: str = Depends(get_user), c: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    if controls.get_role(user, c) != "admin":
        controls.log_action(user, "audit_denied", "audit", "", c)
        raise controls.PermissionDenied("admin role required")
    return [dict(r) for r in c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]


from app import web  # noqa: E402  (page routes; web imports this module, so it is included last)

app.include_router(web.router)
