from pathlib import Path

from app.config import PROJECT_ROOT, load_settings, settings


def test_paths_resolve_under_project_root():
    for p in (settings.data_dir, settings.raw_dir, settings.processed_dir, settings.chroma_dir, settings.db_path):
        assert p.is_absolute()
        assert PROJECT_ROOT in p.parents or p == PROJECT_ROOT


def test_defaults():
    assert settings.llm_provider == "none" or settings.llm_provider
    assert settings.embed_model


def test_env_override(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("CHUNK_MAX_TOKENS=123\n", encoding="utf-8")
    monkeypatch.delenv("CHUNK_MAX_TOKENS", raising=False)
    assert load_settings(env).chunk_max_tokens == 123
