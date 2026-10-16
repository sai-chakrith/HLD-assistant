"""Task 4: warm-up states, DEMO_MODEL default, demo_reset safety."""
import argparse

import pytest
from fastapi.testclient import TestClient

from app import api, llm
from app.config import settings
from scripts import demo_reset


def test_model_split_defaults():
    """Q&A/summaries on qwen3 (kept loaded), extraction on qwen2.5; both allowed."""
    assert settings.demo_model == "qwen3:4b-instruct" and settings.llm_extract_model == "qwen2.5:3b"
    assert {"qwen3:4b-instruct", "qwen2.5:3b"} <= set(settings.allowed_models)
    assert settings.ollama_model == "qwen2.5:3b" and settings.demo_keep_alive == "30m"


def test_demo_helper_prefers_request_model():
    assert api._demo(None) == "qwen3:4b-instruct"
    assert api._demo("notallowed:1b") == "notallowed:1b"


def test_ask_uses_demo_model_when_none_given(monkeypatch, tmp_path, conn, dirs):
    conn.close()
    seen = []

    class R:
        def model_dump(self):
            return {}

    def fake_ask(*a, **k):
        seen.append(k["model"])
        return R()

    monkeypatch.setattr(api.qa, "ask", fake_ask)
    api.app.dependency_overrides[api.get_paths] = lambda: api.Paths(tmp_path / "test.db", dirs["chroma_dir"],
                                                                    dirs["processed_dir"])
    c = TestClient(api.app)
    h = {"X-User": "view"}
    c.post("/projects/p1/ask", headers=h, json={"question": "x"})
    c.post("/projects/p1/ask", headers=h, json={"question": "x", "model": "qwen2.5:3b"})
    api.app.dependency_overrides.clear()
    assert seen == ["qwen3:4b-instruct", "qwen2.5:3b"]


def _health(monkeypatch, state):
    monkeypatch.setattr(api, "WARMUP", state)
    return TestClient(api.app).get("/health").json()["warmup"]


def test_health_warmup_states(monkeypatch):
    assert _health(monkeypatch, {"status": "warming", "steps": {}, "reason": ""})["status"] == "warming"
    assert _health(monkeypatch, {"status": "ready", "steps": {"llm": "ok"}, "reason": ""})["status"] == "ready"
    w = _health(monkeypatch, {"status": "degraded", "steps": {"llm": "failed"}, "reason": "llm: down"})
    assert w["status"] == "degraded" and "down" in w["reason"]


def test_warmup_ready_when_llm_none(monkeypatch):
    monkeypatch.setattr(api, "WARMUP", {"status": "warming", "steps": {}, "reason": ""})
    api._warmup()
    assert api.WARMUP["status"] == "ready" and api.WARMUP["steps"]["llm"] == "skipped"


def test_warmup_degraded_never_raises(monkeypatch):
    monkeypatch.setattr(api, "WARMUP", {"status": "warming", "steps": {}, "reason": ""})

    def boom():
        raise RuntimeError("no model")

    monkeypatch.setattr(api.retrieve, "get_embedder", boom)
    api._warmup()
    assert api.WARMUP["status"] == "degraded" and "no model" in api.WARMUP["reason"]


def _args(**kw):
    d = dict(data_dir=None, db_path=None, chroma_dir=None, processed_dir=None, raw_dir=None)
    d.update(kw)
    return argparse.Namespace(**d)


def test_demo_reset_refuses_without_yes(tmp_path):
    with pytest.raises(demo_reset.Refused):
        demo_reset.main(["--data-dir", str(tmp_path)])
    t = demo_reset.resolve_target(_args(), env={})
    assert t["DB_PATH"] == demo_reset.MAIN_DB.resolve()
    with pytest.raises(demo_reset.Refused, match="main checkout"):
        demo_reset.check_safe(t, yes=False)
    demo_reset.check_safe(t, yes=True)  # allowed only with --yes (nothing is deleted by check_safe)


def test_demo_reset_stops_before_wiping_when_raw_dir_has_no_pdf(tmp_path):
    (tmp_path / "hld.db").write_bytes(b"keep me")
    with pytest.raises(demo_reset.Refused, match="--raw-dir"):
        demo_reset.main(["--yes", "--data-dir", str(tmp_path), "--raw-dir", str(tmp_path / "empty")])
    assert (tmp_path / "hld.db").read_bytes() == b"keep me"  # nothing was deleted
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "doc.pdf").write_bytes(b"%PDF-1.4")
    demo_reset.check_sources(demo_reset.resolve_target(_args(data_dir=str(tmp_path)), env={}))  # no exception


def test_demo_reset_wipe_keeps_llm_cache(tmp_path):
    t = demo_reset.resolve_target(_args(data_dir=str(tmp_path / "d"), chroma_dir=str(tmp_path / "c")), env={})
    t["DB_PATH"].parent.mkdir(parents=True)
    llm.cache_put("k", "m", "p", "resp", t["DB_PATH"])
    t["CHROMA_DIR"].mkdir()
    (t["CHROMA_DIR"] / "x").write_text("1")
    assert demo_reset.wipe(t, keep_cache=True) == 1
    assert llm.cache_get("k", t["DB_PATH"]) == "resp" and not t["CHROMA_DIR"].exists()
    assert demo_reset.wipe(t, keep_cache=False) == 0 and llm.cache_get("k", t["DB_PATH"]) is None


def test_warmup_loads_only_the_qa_model_with_keep_alive(monkeypatch):
    sent = []
    monkeypatch.setattr(api, "WARMUP", {"status": "warming", "steps": {}, "reason": ""})
    monkeypatch.setattr(api.retrieve, "get_embedder", lambda: None)
    monkeypatch.setattr(api, "settings", api.settings.__class__(**{**api.settings.__dict__, "llm_provider": "ollama"}))
    monkeypatch.setattr(llm.OllamaProvider, "_requests_post", staticmethod(lambda url, payload, t: sent.append(payload) or {}))
    api._warmup()
    assert [p["model"] for p in sent] == ["qwen3:4b-instruct"]
    assert sent[0]["keep_alive"] == "30m" and sent[0]["think"] is False and sent[0]["options"]["num_ctx"] == 4096
