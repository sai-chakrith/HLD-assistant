"""Browser UI served by FastAPI: page shells, static files, offline guarantee, role-gated review controls."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import api, web
from tests.test_api import H, _upload  # noqa: F401

APP_DIR = Path(api.__file__).resolve().parent
PAGES = {
    "/": ["ask-form", 'id="question"', 'id="answer"', 'id="history"', 'id="samples"', "Try one of these"],
    "/documents": ["upload-form", "docs-table", "Upload and process"],
    "/architecture": ["doc-select", "inv-tabs", "inv-filter", 'id="graph"', "side-panel", "flow-btn"],
    "/review": ["doc-select", 'id="findings"', "compare-form", 'data-export="inventory"', 'data-export="findings"',
                'data-export="diff"', "rule-names"],
}


@pytest.fixture
def client(tmp_path, conn, dirs):
    conn.close()
    paths = api.Paths(tmp_path / "test.db", dirs["chroma_dir"], dirs["processed_dir"])
    api.app.dependency_overrides[api.get_paths] = lambda: paths
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _as(client, user, path, **params):
    client.cookies.set("hld_user", user)
    return client.get(path, params=params)


@pytest.mark.parametrize("path", list(PAGES))
def test_every_page_renders_with_its_key_elements(client, path):
    r = _as(client, "admin", path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    for needle in PAGES[path]:
        assert needle in r.text, f"{needle!r} missing on {path}"
    # shared header: product name, four screens (Ask first), Settings popover with the selectors, status dot
    for needle in ("HLD Assistant", 'href="/documents"', 'href="/architecture"', 'href="/review"', "project-select",
                   "user-select", "settings-btn", "status-dot", 'data-user="admin"', 'data-project="p1"',
                   'name="color-scheme" content="light"', 'href="#main"'):
        assert needle in r.text
    assert r.text.count('aria-current="page"') == 1
    page_js = {"/": "ask", "/documents": "documents", "/architecture": "architecture", "/review": "review"}[path]
    assert f"/static/js/{page_js}.js" in r.text


def test_removed_screens_are_gone(client):
    html = _as(client, "admin", "/").text
    for gone in ("Evaluation", "Audit", "model-select", "Model ("):
        assert gone not in html
    assert client.get("/evaluation").status_code == 404
    assert client.get("/audit", headers=H("admin")).status_code == 200  # the audit API stays


def test_project_is_chosen_by_query_then_cookie(client):
    assert 'data-project="p2"' in _as(client, "view", "/", project="p2").text
    client.cookies.set("hld_project", "p2")
    assert 'data-project="p2"' in _as(client, "view", "/").text
    assert 'data-project="p1"' in _as(client, "view", "/", project="nope").text  # unknown -> default (first) project


def test_unknown_user_sees_no_project_and_no_page_script(client):
    html = _as(client, "ghost", "/review").text
    assert "which has no project" in html and "Open Settings" in html
    assert "/static/js/review.js" not in html and 'id="findings"' not in html


def test_viewer_gets_no_review_controls(client):
    viewer = _as(client, "view", "/review").text
    assert 'id="review-actions"' not in viewer and 'data-decision="approved"' not in viewer
    assert "Approve" not in viewer and "Reject" not in viewer
    for user in ("rev", "admin"):
        html = _as(client, user, "/review").text
        assert 'id="review-actions"' in html and 'data-decision="approved"' in html and 'data-decision="rejected"' in html


def test_only_admins_get_the_upload_form(client):
    assert "upload-form" in _as(client, "admin", "/documents").text
    viewer = _as(client, "view", "/documents").text
    assert "upload-form" not in viewer and "needs the admin role" in viewer


def test_doc_query_is_passed_to_the_picker(client):
    assert 'data-wanted="hld_v9"' in _as(client, "view", "/architecture", doc="hld_v9").text


def test_static_files_are_served(client):
    css = client.get("/static/css/app.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"] and "IBM Plex Sans" in css.text
    for js in ("api", "ask", "documents", "architecture", "review"):
        r = client.get(f"/static/js/{js}.js")
        assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    for font in ("IBMPlexSans-Regular", "IBMPlexSans-Medium", "IBMPlexSans-SemiBold", "IBMPlexMono-Regular",
                 "IBMPlexMono-Medium"):
        r = client.get(f"/static/vendor/fonts/{font}.woff2")
        assert r.status_code == 200 and r.content[:4] == b"wOF2"
    assert client.get("/static/vendor/fonts/LICENSE-IBM-Plex.txt").status_code == 200
    assert client.get("/static/../api.py").status_code == 404


def test_ui_files_reference_nothing_online():
    files = [*(APP_DIR / "templates").glob("*.html"), *(APP_DIR / "static" / "js").glob("*.js"),
             *(APP_DIR / "static" / "css").glob("*.css")]
    assert len(files) >= 11
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"https?://", text), f"{f.name} references an online resource"
        assert "@import" not in text


def test_ui_has_no_business_logic():
    """Page code only talks to the API; the router only resolves the viewer's role via the API's list_projects."""
    for f in (APP_DIR / "static" / "js").glob("*.js"):
        text = f.read_text(encoding="utf-8")
        assert "localStorage" not in text and "sessionStorage" not in text  # session history is memory only
    src = Path(web.__file__).read_text(encoding="utf-8")
    for banned in ("from app import qa", "from app import checks", "extract", "compare_mod", "summarize", "llm", "SELECT"):
        assert banned not in src


def test_documents_api_reports_pages_and_graph_is_offline(client, tmp_path, make_pdf):
    pdf = make_pdf(tmp_path / "doc1.pdf")
    assert _upload(client, "admin", pdf).status_code == 200
    docs = client.get("/projects/p1/documents", headers=H("view")).json()
    assert docs[0]["pages"] == 4 and docs[0]["uploaded_at"]
    html = client.get("/projects/p1/graph", headers=H("view"), params={"doc_id": "doc1"}).json()["html"]
    assert "new vis.Network" in html
    assert not re.search(r'<(?:script|link)[^>]+(?:src|href)="https?://', html)


def test_rejected_upload_reason_is_returned_for_inline_display(client, tmp_path, make_pdf):
    pdf = make_pdf(tmp_path / "draft.pdf")
    r = _upload(client, "admin", pdf, status="draft")
    assert r.status_code == 400 and "not allowed" in r.json()["detail"]



# ---------------------------------------------------------------- simplified UI: routes, theme, accessibility
def test_home_is_ask_and_old_ask_url_redirects_keeping_the_query(client):
    home = _as(client, "admin", "/")
    assert "<h1>Ask</h1>" in home.text and "/static/js/ask.js" in home.text
    r = client.get("/ask", params={"project": "p2"}, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/?project=p2"
    assert "<h1>Documents</h1>" in _as(client, "admin", "/documents").text


def test_tab_order_is_ask_architecture_review_documents(client):
    html = _as(client, "admin", "/").text
    nav = html[html.index('<nav class="nav"'):html.index("</nav>")]
    assert [nav.index(x) for x in ('href="/"', 'href="/architecture"', 'href="/review"', 'href="/documents"')] == \
        sorted(nav.index(x) for x in ('href="/"', 'href="/architecture"', 'href="/review"', 'href="/documents"'))


@pytest.mark.parametrize("path", list(PAGES))
def test_every_page_has_exactly_one_h1_and_the_project_name(client, path):
    html = _as(client, "admin", path).text
    assert html.count("<h1") == 1
    assert html.count('class="project-name"') == 1


def test_unknown_user_page_also_has_one_h1(client):
    assert _as(client, "ghost", "/").text.count("<h1") == 1


def test_header_has_no_project_select_but_the_settings_popover_does(client):
    html = _as(client, "admin", "/").text
    header = html[html.index('<header class="topbar">'):html.index("</header>")]
    assert "<select" not in header and "Settings" in header and 'aria-expanded="false"' in header
    pop = html[html.index('id="settings"'):html.index("</div>", html.index('id="settings"'))]
    assert 'id="project-select"' in pop and 'id="user-select"' in pop and "demo role" in pop
    assert "Projects keep documents and access separate." in pop


def test_project_display_name_and_default_project():
    assert web.project_title("body-comfort") == "Body Comfort"
    assert web.project_title("synthetic-ecu") == "Synthetic ECU" and web.project_title("hmi") == "HMI"
    assert web.project_title("p1", "Wiper project") == "Wiper project"


def test_default_project_has_most_documents_named_like_it(client, tmp_path, make_pdf, monkeypatch):
    projects = [{"project_id": "alpha"}, {"project_id": "body-comfort"}]
    docs = {"alpha": [{"doc_id": "x1"}], "body-comfort": [{"doc_id": "bodycomfort_r3.1.5"}, {"doc_id": "bodycomfort_r3.2.3"}]}
    monkeypatch.setattr(web, "documents", lambda pid, user, c, paths: docs[pid])
    assert web.default_project(projects, "admin", None, None)["project_id"] == "body-comfort"
    monkeypatch.setattr(web, "documents", lambda pid, user, c, paths: [])
    assert web.default_project(projects, "admin", None, None)["project_id"] == "alpha"  # fallback: the first


def test_theme_uses_local_fonts_and_accessible_light_surface():
    css = (APP_DIR / "static" / "css" / "app.css").read_text(encoding="utf-8")
    assert "color-scheme: light" in css and ":focus-visible" in css
    assert "IBM Plex Sans" in css and "IBM Plex Mono" in css
    assert "prefers-color-scheme" not in css


def test_plain_rule_names_and_readable_method_labels():
    assert web.RULE_NAMES["M1"] == "Port with no provider"
    js = (APP_DIR / "static" / "js" / "api.js").read_text(encoding="utf-8")
    for label in ("from a table", "from a diagram", "read by the model", "guessed from the name", "from headings",
                  "match ${Math.round(v * 100)}%", "Ready", "Warming up", "Offline", "Unreviewed AI output",
                  "Not reviewed yet"):
        assert label in js


def test_tables_and_inputs_are_labelled():
    for f in (APP_DIR / "static" / "js").glob("*.js"):
        text = f.read_text(encoding="utf-8")
        assert 'h("th", {}' not in text, f"{f.name}: header cell without scope"
    for f in (APP_DIR / "templates").glob("*.html"):
        html = f.read_text(encoding="utf-8")
        for m in re.finditer(r'<(?:input|select)[^>]*\bid="([^"]+)"', html):
            ident = m.group(1)
            labelled = f'for="{ident}"' in html or "aria-label" in m.group(0)
            wrapped = re.search(r'<label[^>]*>[^<]*(?:<[^/][^>]*>)*\s*<(?:input|select)[^>]*id="' + ident, html)
            assert labelled or wrapped, f"{f.name}: #{ident} has no label"


def test_no_ai_look_wording():
    banned = ("AI-powered", "Unlock", "Supercharge", "Seamless", "Powerful", "Insights", "lorem", "\u2728")
    for f in [*(APP_DIR / "templates").glob("*.html"), *(APP_DIR / "static" / "js").glob("*.js")]:
        text = f.read_text(encoding="utf-8")
        for w in banned:
            assert w not in text, f"{w!r} in {f.name}"


def test_answer_region_is_live_and_side_panel_is_a_modal_dialog(client):
    assert 'id="answer" class="answer" aria-live="polite"' in _as(client, "admin", "/").text
    arch = _as(client, "admin", "/architecture").text
    assert 'role="dialog" aria-modal="true"' in arch and 'role="tablist"' in arch
    js = (APP_DIR / "static" / "js" / "architecture.js").read_text(encoding="utf-8")
    assert '"Escape"' in js and '"Tab"' in js  # Esc closes, Tab is kept inside the panel
