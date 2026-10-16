"""Phase G endpoints: page PNG (isolation), graph filter, eval endpoints, /me, answer model field."""
import pytest
from fastapi.testclient import TestClient

from app import api, controls
from app.config import settings
from tests.test_api import H, _upload  # noqa: F401


@pytest.fixture
def client(tmp_path, conn, dirs):
    conn.close()
    paths = api.Paths(tmp_path / "test.db", dirs["chroma_dir"], dirs["processed_dir"])
    api.app.dependency_overrides[api.get_paths] = lambda: paths
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


@pytest.fixture
def uploaded(client, tmp_path, make_pdf):
    pdf = tmp_path / "doc1.pdf"
    make_pdf(pdf)
    assert _upload(client, "admin", pdf).status_code == 200
    return "doc1"


def test_default_model_is_qwen_and_allowed():
    assert settings.ollama_model == "qwen3:4b-instruct"
    assert "qwen3:4b-instruct" in settings.allowed_models


def test_page_png_member_ok_and_isolated(client, uploaded):
    r = client.get(f"/projects/p1/documents/{uploaded}/pages/1.png", headers=H("view"))
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get(f"/projects/p1/documents/{uploaded}/pages/1.png?highlight=Port", headers=H("view")).status_code == 200
    # same doc id asked through another project: not found there
    assert client.get(f"/projects/p2/documents/{uploaded}/pages/1.png", headers=H("view")).status_code == 404
    assert client.get(f"/projects/p1/documents/{uploaded}/pages/999.png", headers=H("view")).status_code == 404
    assert client.get(f"/projects/p1/documents/{uploaded}/pages/1.png").status_code == 401
    db = controls.get_conn(api.app.dependency_overrides[api.get_paths]().db_path)
    controls.add_user("outsider", "viewer", conn=db)
    db.close()
    assert client.get(f"/projects/p1/documents/{uploaded}/pages/1.png", headers=H("outsider")).status_code == 403


def test_graph_component_filter(client, uploaded):
    full = client.get("/projects/p1/graph", headers=H("view"), params={"doc_id": uploaded}).json()
    assert full["total_nodes"] == full["nodes"] and full["components"]
    comp = full["components"][0]
    sub = client.get("/projects/p1/graph", headers=H("view"),
                     params={"doc_id": uploaded, "component": comp, "depth": 1}).json()
    assert sub["component"] == comp and 1 <= sub["nodes"] <= full["nodes"]
    assert sub["total_nodes"] == full["total_nodes"]
    assert all(e["id"] in {n["id"] for n in sub["json"]["nodes"]} for e in sub["evidence"])
    assert client.get("/projects/p1/graph", headers=H("view"),
                      params={"doc_id": uploaded, "component": "NoSuchThing"}).status_code == 404


def test_eval_endpoints(client):
    r = client.get("/eval/results", headers=H("view"))
    assert r.status_code == 200
    body = r.json()
    assert "Evaluation results" in body["markdown"] and body["charts"]
    c = client.get(f"/eval/charts/{body['charts'][0]}", headers=H("view"))
    assert c.status_code == 200 and c.content[:4] == b"\x89PNG"
    assert client.get("/eval/charts/..%2Fresults.md", headers=H("view")).status_code in (404, 422)
    assert client.get("/eval/charts/nope.png", headers=H("view")).status_code == 404
    assert client.get("/eval/results", headers=H("ghost")).status_code == 403
    assert client.get("/eval/results").status_code == 401


def test_me_and_audit_admin_only(client):
    assert client.get("/me", headers=H("admin")).json()["is_admin"] is True
    assert client.get("/me", headers=H("view")).json()["is_admin"] is False
    assert client.get("/audit", headers=H("view")).status_code == 403
