"""Browser UI: server-rendered page shells (Jinja2) for the four screens. No business logic lives here.

Each page is a static layout; the data on it is loaded by app/static/js/*.js from the JSON API with the X-User header.
The only things resolved on the server are who is looking (cookie ``hld_user``, default admin), the selected project
(``?project=``, else cookie ``hld_project``, else the default project) and the viewer's role in it, using the API's own
``list_projects`` / ``documents``. That lets the review page leave the approve / reject controls out of the HTML for
viewers; the API still enforces the role on every call.

Import order: this module imports ``app.api`` and ``app.api`` includes ``router`` at its end, so import ``app.api``
(not this module) first.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth, controls
from app.api import Paths, documents, get_db, get_paths, list_projects

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

USERS = ("admin", "reviewer", "viewer")  # seeded demo roles (scripts/seed.py)
DEFAULT_USER = "admin"
DOC_TYPES = ("hld", "interface_spec", "architecture", "catalogue")
# Upload choices. "draft" is offered on purpose: the server rejects it, which is how the ingestion control is shown.
SOURCE_STATUSES = ("approved", "synthetic", "draft")
# Plain names for the rule ids in app/checks.py (labels only; the rules themselves live in checks.py).
RULE_NAMES = {
    "M1": "Port with no provider",
    "M2": "Port kind does not match its interface",
    "M3": "Signal with no sender or receiver",
    "M4": "Names that differ only slightly",
    "C1": "Component not in any functional flow",
    "C2": "Port with no interface",
    "C3": "Interface used but never defined",
    "C4": "Component with no description",
}
PAGES = (("ask", "/", "Ask"), ("architecture", "/architecture", "Architecture"), ("review", "/review", "Review"),
         ("documents", "/documents", "Documents"))


def project_title(project_id: str, name: str | None = None) -> str:
    """'body-comfort' -> 'Body Comfort', 'synthetic-ecu' -> 'Synthetic ECU' (a stored name that differs wins)."""
    if name and name != project_id:
        return name
    words = [w for w in re.split(r"[-_\s]+", project_id or "") if w]
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in words)


def default_project(projects: list[dict], user: str, c: sqlite3.Connection, paths: Paths) -> dict | None:
    """The project with the most documents whose id starts like the project id (body-comfort -> bodycomfort_*),
    else the first one."""
    best, best_n = (projects[0] if projects else None), 0
    for p in projects:
        key = re.sub(r"[^a-z0-9]", "", p["project_id"].lower())
        try:
            docs = documents(p["project_id"], user, c, paths)
        except Exception:  # noqa: BLE001 - a project the user cannot read simply does not win
            continue
        n = sum(1 for d in docs if re.sub(r"[^a-z0-9]", "", d["doc_id"].lower()).startswith(key))
        if n > best_n:
            best, best_n = p, n
    return best


def _context(request: Request, page: str, c: sqlite3.Connection, paths: Paths) -> dict:
    user = auth.identify(request, request.cookies.get("hld_user") or DEFAULT_USER)
    try:
        projects = list_projects(user, c)
    except controls.PermissionDenied:
        projects = []
    want = request.query_params.get("project") or request.cookies.get("hld_project")
    project = next((p for p in projects if p["project_id"] == want), None)
    if project is None:
        project = default_project(projects, user, c, paths)
    role = project["role"] if project else None
    return {"request": request, "page": page, "pages": PAGES, "user": user,
            "users": (user,) if auth.enabled() else (USERS if user in USERS else (*USERS, user)),
            "secure_auth": auth.enabled(),
            "projects": [{**p, "title": project_title(p["project_id"], p.get("name"))} for p in projects],
            "project": project["project_id"] if project else "",
            "project_name": project_title(project["project_id"], project.get("name")) if project else "",
            "role": role or "", "can_upload": role == "admin", "can_review": role in ("reviewer", "admin"),
            "doc": request.query_params.get("doc", "")}


def _render(request: Request, template: str, page: str, c: sqlite3.Connection, paths: Paths,
            **extra) -> HTMLResponse:
    try:
        context = _context(request, page, c, paths)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        raise
    return templates.TemplateResponse(request, template, {**context, **extra})


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/", response_class=HTMLResponse)
def ask_page(request: Request, c: sqlite3.Connection = Depends(get_db),
             paths: Paths = Depends(get_paths)) -> HTMLResponse:
    return _render(request, "ask.html", "ask", c, paths)


@router.get("/ask")
def ask_redirect(request: Request) -> RedirectResponse:
    q = request.url.query
    return RedirectResponse("/" + (f"?{q}" if q else ""), status_code=307)


@router.get("/documents", response_class=HTMLResponse)
def documents_page(request: Request, c: sqlite3.Connection = Depends(get_db),
                   paths: Paths = Depends(get_paths)) -> HTMLResponse:
    return _render(request, "documents.html", "documents", c, paths, doc_types=DOC_TYPES,
                   source_statuses=SOURCE_STATUSES)


@router.get("/architecture", response_class=HTMLResponse)
def architecture_page(request: Request, c: sqlite3.Connection = Depends(get_db),
                      paths: Paths = Depends(get_paths)) -> HTMLResponse:
    return _render(request, "architecture.html", "architecture", c, paths)


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, c: sqlite3.Connection = Depends(get_db),
                paths: Paths = Depends(get_paths)) -> HTMLResponse:
    return _render(request, "review.html", "review", c, paths, rule_names=RULE_NAMES)
