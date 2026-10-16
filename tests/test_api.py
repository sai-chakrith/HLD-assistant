"""FastAPI behaviour with an isolated DB / chroma / processed dir."""
import pytest
from fastapi.testclient import TestClient

from app import api, controls


def H(u):
    return {"X-User": u}


@pytest.fixture
def client(tmp_path, conn, dirs):
    conn.close()  # the API reopens the same DB file
    paths = api.Paths(tmp_path / "test.db", dirs["chroma_dir"], dirs["processed_dir"])
    api.app.dependency_overrides[api.get_paths] = lambda: paths
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _upload(client, user, path, status="approved", pid="p1"):
    with open(path, "rb") as f:
        return client.post(f"/projects/{pid}/upload", headers=H(user),
                           files={"file": (path.name, f, "application/pdf")},
                           data={"doc_type": "hld", "version": "1.0", "source_status": status})


def test_health_and_auth(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["llm_mode"] == "none" and "reachable" in h["ollama"]
    assert h["allowed_models"] == ["qwen3:4b-instruct"]
    assert client.get("/projects").status_code == 401
    assert client.get("/projects", headers=H("ghost")).status_code == 403
    assert [p["project_id"] for p in client.get("/projects", headers=H("view")).json()] == ["p1", "p2"]


def test_membership_enforced(client, tmp_path):
    db = controls.get_conn(tmp_path / "test.db")
    controls.add_user("outsider", "admin", conn=db)
    db.close()
    for path in ("documents", "inventory", "findings", "graph"):
        assert client.get(f"/projects/p1/{path}", headers=H("outsider")).status_code == 403
    assert client.post("/projects/p1/ask", headers=H("outsider"), json={"question": "x"}).status_code == 403


def test_viewer_cannot_review_or_check(client):
    r = client.post("/projects/p1/review", headers=H("view"),
                    json={"item_type": "finding", "item_id": 1, "decision": "approved"})
    assert r.status_code == 403
    assert client.post("/projects/p1/run-checks", headers=H("view")).status_code == 403
    assert client.get("/projects/p1/documents", headers=H("view")).status_code == 200


def test_upload_rules(client, tmp_path, make_pdf):
    pdf = make_pdf(tmp_path / "doc.pdf")
    assert _upload(client, "view", pdf).status_code == 403
    assert _upload(client, "admin", pdf, status="draft").status_code == 400
    ok = _upload(client, "admin", pdf)
    assert ok.status_code == 200 and ok.json()["doc_id"] == "doc"
    assert _upload(client, "admin", pdf).status_code == 400  # duplicate
    docs = client.get("/projects/p1/documents", headers=H("view")).json()
    assert [d["doc_id"] for d in docs] == ["doc"]
    assert client.get("/projects/p2/documents", headers=H("view")).json() == []  # project isolation


def test_ask_inventory_checks_review_export(client, tmp_path, make_pdf):
    _upload(client, "admin", make_pdf(tmp_path / "doc.pdf"))
    r = client.post("/projects/p1/ask", headers=H("view"), json={"question": "What does the WiperManager do?"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] in ("evidence_only", "refused") and body["confidence"] == "Low"
    if body["mode"] == "evidence_only":
        assert body["citations"]
    inv = client.get("/projects/p1/inventory", headers=H("view")).json()
    assert set(inv) == {"components", "ports", "interfaces", "signals"}
    assert client.post("/projects/p1/run-checks", headers=H("rev")).status_code == 200
    fl = client.get("/projects/p1/findings", headers=H("view")).json()
    assert fl
    rv = client.post("/projects/p1/review", headers=H("rev"),
                     json={"item_type": "finding", "item_id": fl[0]["id"], "decision": "approved", "comment": "ok"})
    assert rv.status_code == 200
    assert client.get("/projects/p1/findings", headers=H("view")).json()[0]["review_status"] == "approved"
    assert client.post("/projects/p1/review", headers=H("rev"),
                       json={"item_type": "finding", "item_id": 99999, "decision": "approved"}).status_code == 404
    ex = client.get("/projects/p1/export", headers=H("view"), params={"kind": "findings", "fmt": "csv"})
    assert ex.status_code == 200 and len(ex.content) > 0
    assert client.get("/projects/p1/graph", headers=H("view")).json()["html"]
    assert client.get("/projects/p1/reports/flow", headers=H("view")).status_code == 200
    assert client.get("/projects/p1/reports/component", headers=H("view"),
                      params={"component": "NoSuchThing"}).status_code == 404


def test_audit_admin_only(client):
    assert client.get("/audit", headers=H("rev")).status_code == 403
    assert client.get("/audit", headers=H("view")).status_code == 403
    r = client.get("/audit", headers=H("admin"))
    assert r.status_code == 200 and any(x["action"] == "audit_denied" for x in r.json())


def test_ask_rejects_unknown_model(client, make_pdf, tmp_path):
    r = client.post("/projects/p1/ask", headers=H("view"), json={"question": "x", "model": "gpt-4"})
    assert r.status_code == 422
    r = client.post("/projects/p1/ask", headers=H("view"), json={"question": "x", "model": "qwen3:4b-instruct"})
    assert r.status_code == 200


# ---------------------------------------------------------------- Phase E
def test_upload_runs_extraction_and_checks(client, tmp_path, make_pdf):
    r = _upload(client, "admin", make_pdf(tmp_path / "doc.pdf"))
    assert r.status_code == 200
    body = r.json()
    assert body["use_llm"] is True and set(body["extracted"]) == {"components", "ports", "interfaces", "signals"}
    assert {"ingest_ms", "extract_ms", "checks_ms", "rewarm_ms"} <= set(body["timings"])
    assert body["rewarm"] is None  # no Ollama provider in the tests: nothing to re-warm
    assert body["extracted"]["components"] >= 1 and body["extracted"]["ports"] >= 1
    assert isinstance(body["findings"], int) and body["findings"] >= 1
    assert len(client.get("/projects/p1/findings", headers=H("view")).json()) == body["findings"]


def test_reextraction_keeps_review_decisions_via_api(client, tmp_path, make_pdf):
    _upload(client, "admin", make_pdf(tmp_path / "doc.pdf"))
    inv = client.get("/projects/p1/inventory", headers=H("view")).json()
    comp = inv["components"][0]
    rv = client.post("/projects/p1/review", headers=H("rev"),
                     json={"item_type": "component", "item_id": comp["id"], "decision": "approved", "comment": "ok"})
    assert rv.status_code == 200
    r = client.post("/projects/p1/extract", headers=H("admin"), json={"doc_id": "doc"})
    assert r.status_code == 200 and r.json()["findings"] >= 1
    inv2 = client.get("/projects/p1/inventory", headers=H("view")).json()
    same = next(c for c in inv2["components"] if c["id"] == comp["id"])
    assert same["review_status"] == "approved" and same["name"] == comp["name"]
    assert len(inv2["components"]) == len(inv["components"])
    assert client.post("/projects/p1/extract", headers=H("rev"), json={"doc_id": "doc"}).status_code == 403


def test_per_project_roles(client, tmp_path):
    db = controls.get_conn(tmp_path / "test.db")
    controls.add_member("p2", "rev", "viewer", conn=db)  # rev: reviewer globally, viewer on p2
    db.close()
    projs = {p["project_id"]: p["role"] for p in client.get("/projects", headers=H("rev")).json()}
    assert projs == {"p1": "reviewer", "p2": "viewer"}
    assert client.post("/projects/p1/run-checks", headers=H("rev")).status_code == 200
    assert client.post("/projects/p2/run-checks", headers=H("rev")).status_code == 403
    assert client.get("/projects/p2/documents", headers=H("rev")).status_code == 200


def test_require_role_uses_membership_role_and_migration(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)  # pre-migration schema: no project_members.role
    c.executescript("CREATE TABLE projects(project_id TEXT PRIMARY KEY, name TEXT, created_at TEXT);"
                    "CREATE TABLE users(username TEXT PRIMARY KEY, role TEXT NOT NULL);"
                    "CREATE TABLE project_members(project_id TEXT NOT NULL, username TEXT NOT NULL,"
                    " PRIMARY KEY(project_id, username));"
                    "INSERT INTO projects VALUES('a','a',''),('b','b','');"
                    "INSERT INTO users VALUES('u','reviewer'),('g','admin');"
                    "INSERT INTO project_members VALUES('a','u'),('b','u');")
    c.commit()
    c.close()
    conn = controls.get_conn(db)
    assert controls.get_member_role("u", "a", conn) == "reviewer"  # copied from users.role
    controls.add_member("b", "u", "viewer", conn=conn)
    assert controls.require_role("u", "a", "reviewer", conn) == "reviewer"
    with pytest.raises(controls.PermissionDenied):
        controls.require_role("u", "b", "reviewer", conn)
    with pytest.raises(controls.PermissionDenied):  # global admin that is not a member stays isolated
        controls.require_role("g", "a", "viewer", conn)
    conn.close()


def test_seed_demonstrates_per_project_roles(tmp_path):
    import importlib
    seed = importlib.import_module("scripts.seed")
    conn = controls.get_conn(tmp_path / "s.db")
    seed.seed(conn)
    assert controls.get_member_role("reviewer", "chassis", conn) == "viewer"
    assert controls.get_member_role("reviewer", "hmi", conn) == "reviewer"
    conn.close()


def test_export_diff_and_report(client, tmp_path, make_pdf):
    pdf = make_pdf(tmp_path / "doc.pdf")
    _upload(client, "admin", pdf)
    pdf2 = make_pdf(tmp_path / "doc2.pdf", marker="beta")
    with open(pdf2, "rb") as f:
        client.post("/projects/p1/upload", headers=H("admin"), files={"file": ("doc2.pdf", f, "application/pdf")},
                    data={"doc_type": "hld", "version": "2.0", "source_status": "approved"})
    d = client.get("/projects/p1/export/diff", headers=H("view"), params={"doc_a": "doc", "doc_b": "doc2", "fmt": "json"})
    assert d.status_code == 200 and b"review_status" in d.content
    x = client.get("/projects/p1/export/diff", headers=H("view"), params={"doc_a": "doc", "doc_b": "doc2", "fmt": "xlsx"})
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert client.get("/projects/p1/export/diff", headers=H("view"),
                      params={"doc_a": "doc", "doc_b": "nope"}).status_code == 404
    assert client.get("/projects/p1/reports/flow", headers=H("view"), params={"doc_id": "doc"}).status_code == 200
    reports = client.get("/projects/p1/reports", headers=H("view")).json()
    assert reports and reports[0]["review_status"] == "unreviewed"
    rid = reports[0]["id"]
    e = client.get("/projects/p1/export/report", headers=H("view"), params={"report_id": rid, "fmt": "csv"})
    assert e.status_code == 200 and len(e.content) > 0
    j = client.get("/projects/p1/export/report", headers=H("view"), params={"report_id": rid, "fmt": "json"})
    assert j.status_code == 200 and b"review_status" in j.content
    # other project / outsider cannot export it
    assert client.get("/projects/p2/export/report", headers=H("view"), params={"report_id": rid}).status_code == 404


def test_health_reports_embedder(client):
    h = client.get("/health").json()
    assert h["embedder"]["kind"] == "hash" and h["embedder"]["fallback"] is True


def test_request_connection_can_close_on_another_thread(tmp_path):
    """FastAPI may run a sync yield-dependency's setup and teardown on different worker threads."""
    import threading

    from app import api
    gen = api.get_db(api.Paths(tmp_path / "t.db", tmp_path / "c", tmp_path / "p"))
    holder = {}
    t = threading.Thread(target=lambda: holder.setdefault("c", next(gen)))
    t.start()
    t.join()
    holder["c"].execute("SELECT 1")
    gen.close()  # closes the connection on this (different) thread without ProgrammingError



def test_rewarm_is_skipped_when_extraction_uses_the_same_model(monkeypatch):
    from app import api, llm
    calls = []

    class P(llm.OllamaProvider):
        def warm(self, model=None, purpose="warmup"):
            calls.append((model, purpose, api.WARMUP["status"]))
            return {"model": model, "latency_ms": 9000, "load_ms": 8500}

    monkeypatch.setattr(api, "WARMUP", {"status": "ready", "steps": {}, "reason": ""})
    out = api.rewarm_qa_model(P(http_post=lambda *a: {}))
    assert calls == [] and out is None
    assert api.WARMUP["status"] == "ready"
    assert api.rewarm_qa_model(llm.NullProvider()) is None
