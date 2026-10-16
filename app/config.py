"""Single settings object for the app; reads .env and resolves paths against the project root."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    raw_dir: Path
    processed_dir: Path
    synthetic_dir: Path
    chroma_dir: Path
    db_path: Path
    embed_model: str
    llm_provider: str
    ollama_base_url: str
    ollama_model: str
    demo_model: str
    demo_keep_alive: str
    similarity_threshold: float
    high_score: float
    trace_path: Path
    allowed_models: tuple
    fuzzy_threshold: float
    chunk_target_tokens: int
    chunk_max_tokens: int
    chunk_overlap_tokens: int
    diag_min_box: float
    diag_join_tol: float
    diag_touch_tol: float
    diag_high_dist: float
    diag_label_arrow_dist: float
    diag_high_label_dist: float
    diag_port_inside_margin: float
    diag_port_asym: float
    diag_port_outside_dist: float
    diag_port_arrow_y: float
    llm_extract_min_chars: int
    llm_extract_chunk_chars: int
    llm_extract_model: str
    ocr_enabled: bool
    max_upload_mb: float
    tesseract_cmd: str
    retrieval_hybrid: bool
    retrieval_query_understanding: bool
    retrieval_inventory: bool
    retrieval_expand: bool
    retrieval_rerank: bool
    retrieval_candidates: int
    retrieval_context_tokens: int
    rerank_model: str


def _flag(value: str) -> bool:
    return value.strip().lower() not in ("0", "false", "no", "off")


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    g = os.environ.get
    data_dir = _path(g("DATA_DIR", "data"))
    return Settings(
        project_root=PROJECT_ROOT,
        data_dir=data_dir,
        raw_dir=_path(g("RAW_DIR", str(data_dir / "raw"))),
        processed_dir=_path(g("PROCESSED_DIR", str(data_dir / "processed"))),
        synthetic_dir=_path(g("SYNTHETIC_DIR", str(data_dir / "synthetic"))),
        chroma_dir=_path(g("CHROMA_DIR", "chroma")),
        db_path=_path(g("DB_PATH", str(data_dir / "hld.db"))),
        embed_model=g("EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
        llm_provider=g("LLM_PROVIDER", "ollama").lower(),
        ollama_base_url=g("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=g("OLLAMA_MODEL", "qwen2.5:3b"),
        demo_model=g("DEMO_MODEL", "qwen3:4b-instruct"),
        demo_keep_alive=g("DEMO_KEEP_ALIVE", "30m"),
        similarity_threshold=float(g("SIMILARITY_THRESHOLD", "0.65")),
        high_score=float(g("HIGH_SCORE", "0.75")),
        trace_path=_path(g("TRACE_PATH", str(data_dir / "traces.jsonl"))),
        allowed_models=tuple(m.strip() for m in g("ALLOWED_MODELS", "qwen2.5:3b,qwen3:4b-instruct").split(",") if m.strip()),
        fuzzy_threshold=float(g("FUZZY_THRESHOLD", "88")),
        chunk_target_tokens=int(g("CHUNK_TARGET_TOKENS", "500")),
        chunk_max_tokens=int(g("CHUNK_MAX_TOKENS", "600")),
        chunk_overlap_tokens=int(g("CHUNK_OVERLAP_TOKENS", "50")),
        diag_min_box=float(g("DIAG_MIN_BOX", "22")),
        diag_join_tol=float(g("DIAG_JOIN_TOL", "1.5")),
        diag_touch_tol=float(g("DIAG_TOUCH_TOL", "4")),
        diag_high_dist=float(g("DIAG_HIGH_DIST", "3")),
        diag_label_arrow_dist=float(g("DIAG_LABEL_ARROW_DIST", "9")),
        diag_high_label_dist=float(g("DIAG_HIGH_LABEL_DIST", "7")),
        diag_port_inside_margin=float(g("DIAG_PORT_INSIDE_MARGIN", "20")),
        diag_port_asym=float(g("DIAG_PORT_ASYM", "8")),
        diag_port_outside_dist=float(g("DIAG_PORT_OUTSIDE_DIST", "16")),
        diag_port_arrow_y=float(g("DIAG_PORT_ARROW_Y", "6")),
        llm_extract_min_chars=int(g("LLM_EXTRACT_MIN_CHARS", "250")),
        llm_extract_chunk_chars=int(g("LLM_EXTRACT_CHUNK_CHARS", "3000")),
        llm_extract_model=g("LLM_EXTRACT_MODEL", "qwen2.5:3b"),
        ocr_enabled=g("OCR_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off"),
        max_upload_mb=float(g("MAX_UPLOAD_MB", "50")),
        tesseract_cmd=g("TESSERACT_CMD", "").strip(),
        retrieval_hybrid=_flag(g("RETRIEVAL_HYBRID", "true")),
        retrieval_query_understanding=_flag(g("RETRIEVAL_QUERY_UNDERSTANDING", "true")),
        retrieval_inventory=_flag(g("RETRIEVAL_INVENTORY", "true")),
        retrieval_expand=_flag(g("RETRIEVAL_EXPAND", "false")),
        retrieval_rerank=_flag(g("RETRIEVAL_RERANK", "false")),
        retrieval_candidates=int(g("RETRIEVAL_CANDIDATES", "20")),
        retrieval_context_tokens=int(g("RETRIEVAL_CONTEXT_TOKENS", "2800")),
        rerank_model=g("RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
    )


settings = load_settings()
