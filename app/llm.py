"""LLM provider abstraction.

LOCAL MODELS ONLY: the only real provider is a local Ollama server (default http://localhost:11434).
No cloud APIs, no API keys. Cloud provider names are rejected.

Every Ollama call is (1) looked up in / stored to the ``llm_cache`` SQLite table, keyed by
sha256(model + system + prompt + schema), and (2) appended to a local JSONL trace log
(ts, model, purpose, prompt_hash, chunk_ids, latency_ms, cached, ok, error).

Callers must catch LLMNotConfigured (Ollama unreachable / timeout / no provider) and degrade gracefully.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

import requests
from pydantic import BaseModel, ValidationError

from app.config import settings

OPTIONS = {"temperature": 0.1, "num_ctx": 4096}
MAX_RETRIES = 2  # validation retries after the first attempt
TIMEOUT_S = 180

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache(
  key TEXT PRIMARY KEY, model TEXT, purpose TEXT, response TEXT, created_at TEXT);
"""


class LLMNotConfigured(RuntimeError):
    """Raised when an LLM call is attempted but no provider is reachable/configured."""


class ModelNotAllowed(ValueError):
    """Raised for a model outside settings.allowed_models."""


def validate_model(model: str | None) -> str:
    """Return the model (default settings.ollama_model) or raise ModelNotAllowed."""
    m = model or settings.ollama_model
    if m not in settings.allowed_models:
        raise ModelNotAllowed(f"model {m!r} not allowed; choose one of {list(settings.allowed_models)}")
    return m


_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


def strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning blocks (thinking models) so JSON/citation parsing sees only the answer."""
    return _THINK.sub("", text or "").strip() if "<think>" in (text or "") else (text or "")


def thinking_off(model: str) -> bool:
    """Every call sends think=false (qwen3 tags can think; qwen2.5 accepts the flag), so outputs stay
    schema/citation parseable; <think> blocks are stripped as well."""
    return True


# purpose -> model routing: interactive work (Q&A, summaries, reports) on DEMO_MODEL, extraction and anything run at
# upload time on LLM_EXTRACT_MODEL; other purposes on the provider's model (OLLAMA_MODEL).
INTERACTIVE_PURPOSES = ("qa", "summary", "report", "component_report", "doc_summary", "flow", "warmup")
EXTRACT_PURPOSES = ("extract", "upload")


def model_for(purpose: str = "", override: str | None = None, default: str | None = None) -> str:
    """The model for a call: an explicit override (must be in ALLOWED_MODELS), else routed by purpose."""
    if override:
        return validate_model(override)
    p = (purpose or "").lower()
    if p.startswith(EXTRACT_PURPOSES):
        return settings.llm_extract_model or settings.ollama_model
    if p.startswith(INTERACTIVE_PURPOSES):
        return settings.demo_model
    return default or settings.ollama_model


_LAST_MODEL: dict[str, str | None] = {"model": None}  # last model this process sent a call to
SWAP_LOG_MS = 1000  # an Ollama load_duration above this is logged as a model load/swap


def cache_read_enabled() -> bool:
    """LLM_CACHE_READ=0 regenerates every call (responses are still stored): used for timed evaluation runs."""
    return os.environ.get("LLM_CACHE_READ", "1") != "0"


# ---------------------------------------------------------------- cache helpers
def schema_dict(schema: type[BaseModel] | dict | None) -> dict | None:
    if schema is None:
        return None
    return schema if isinstance(schema, dict) else schema.model_json_schema()


def cache_key(model: str, system: str, prompt: str, schema: type[BaseModel] | dict | None = None) -> str:
    sd = schema_dict(schema)
    blob = json.dumps([model, system, prompt, sd], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_conn(db_path: Path | str | None) -> sqlite3.Connection:
    p = Path(db_path or settings.db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=30)
    c.executescript(CACHE_SCHEMA)
    return c


def cache_get(key: str, db_path: Path | str | None = None) -> str | None:
    c = _cache_conn(db_path)
    try:
        row = c.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        c.close()


def cache_put(key: str, model: str, purpose: str, response: str, db_path: Path | str | None = None) -> None:
    c = _cache_conn(db_path)
    try:
        c.execute("INSERT OR REPLACE INTO llm_cache(key,model,purpose,response,created_at) VALUES(?,?,?,?,?)",
                  (key, model, purpose, response, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        c.commit()
    finally:
        c.close()


def cache_clear(db_path: Path | str | None = None) -> None:
    c = _cache_conn(db_path)
    try:
        c.execute("DELETE FROM llm_cache")
        c.commit()
    finally:
        c.close()


def write_trace(entry: dict, trace_path: Path | str | None = None) -> None:
    p = Path(trace_path or settings.trace_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # tracing must never break a call


# ---------------------------------------------------------------- providers
@runtime_checkable
class Provider(Protocol):
    """generate() returns text, or a parsed dict when a schema (pydantic model / JSON schema) is given."""

    name: str

    def generate(self, prompt: str, system: str = "", schema: type[BaseModel] | dict | None = None,
                 model: str | None = None, purpose: str = "", chunk_ids: list | None = None,
                 use_cache: bool = True) -> str | dict: ...


class _CompleteMixin:
    def complete(self, prompt: str, system: str = "", **kwargs) -> str:
        """Backward-compatible text-only alias for generate()."""
        out = self.generate(prompt, system, **kwargs)  # type: ignore[attr-defined]
        return out if isinstance(out, str) else json.dumps(out)


class NullProvider(_CompleteMixin):
    """Provider used when no LLM is configured (tests / offline without Ollama): every call raises."""

    name = "none"

    def generate(self, prompt: str, system: str = "", schema=None, model: str | None = None, purpose: str = "",
                 chunk_ids: list | None = None, use_cache: bool = True) -> str | dict:
        raise LLMNotConfigured("No LLM provider configured (set LLM_PROVIDER=ollama).")


class OllamaProvider(_CompleteMixin):
    """Local Ollama server. Model: qwen2.5:3b (default), overridable per call among ALLOWED_MODELS.

    ``http_post(url, payload, timeout)`` returns the decoded JSON body; injectable for tests.
    """

    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None, db_path: Path | str | None = None,
                 trace_path: Path | str | None = None, http_post: Callable | None = None,
                 timeout: float = TIMEOUT_S) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.db_path = db_path
        self.trace_path = trace_path
        self.timeout = timeout
        self._post = http_post or self._requests_post

    @staticmethod
    def _requests_post(url: str, payload: dict, timeout: float) -> dict:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _call(self, payload: dict) -> str:
        try:
            body = self._post(f"{self.base_url}/api/generate", payload, self.timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise LLMNotConfigured(f"Ollama unreachable at {self.base_url}: {e}") from e
        except requests.RequestException as e:
            raise LLMNotConfigured(f"Ollama request failed: {e}") from e
        self._note_load(payload["model"], body)
        return strip_think(body.get("response", ""))

    def _note_load(self, model: str, body: dict) -> None:
        """Trace a model load (Ollama load_duration > SWAP_LOG_MS) as purpose 'model_swap' with the previous model."""
        load_ms = (body.get("load_duration") or 0) / 1e6
        prev = _LAST_MODEL["model"]
        _LAST_MODEL["model"] = model
        if load_ms > SWAP_LOG_MS:
            write_trace({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": model,
                         "purpose": "model_swap", "from_model": prev, "latency_ms": int(load_ms), "cached": False,
                         "ok": True, "error": ""}, self.trace_path)

    def payload(self, m: str, prompt: str, system: str = "", **options) -> dict:
        """Request body: think=false always; DEMO_MODEL gets keep_alive=DEMO_KEEP_ALIVE."""
        p = {"model": m, "prompt": prompt, "system": system, "stream": False, "options": {**OPTIONS, **options},
             "think": False}
        if m == settings.demo_model:  # the interactive (Q&A / summaries) model stays loaded between questions
            p["keep_alive"] = settings.demo_keep_alive
        return p

    def warm(self, model: str | None = None, purpose: str = "warmup") -> dict:
        """1-token call that loads (or keeps) a model; returns {model, latency_ms, load_ms} and traces it."""
        m = model or settings.demo_model
        t0 = time.perf_counter()
        body = self._post(f"{self.base_url}/api/generate", self.payload(m, "hi", num_predict=1), self.timeout)
        self._note_load(m, body)
        out = {"model": m, "latency_ms": int((time.perf_counter() - t0) * 1000),
               "load_ms": int((body.get("load_duration") or 0) / 1e6)}
        write_trace({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": m, "purpose": purpose,
                     "latency_ms": out["latency_ms"], "load_ms": out["load_ms"], "cached": False, "ok": True,
                     "error": ""}, self.trace_path)
        return out

    def generate(self, prompt: str, system: str = "", schema: type[BaseModel] | dict | None = None,
                 model: str | None = None, purpose: str = "", chunk_ids: list | None = None,
                 use_cache: bool = True) -> str | dict:
        m = model_for(purpose, model, default=self.model)
        key = cache_key(m, system, prompt, schema)
        t0 = time.perf_counter()
        trace = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": m, "purpose": purpose,
                 "prompt_hash": key[:16], "chunk_ids": list(chunk_ids or []), "cached": False}

        def done(result, ok=True, error=""):
            write_trace({**trace, "latency_ms": int((time.perf_counter() - t0) * 1000), "ok": ok,
                         "error": error}, self.trace_path)
            return result

        try:
            if use_cache and cache_read_enabled():
                hit = cache_get(key, self.db_path)
                if hit is not None:
                    trace["cached"] = True
                    return done(json.loads(hit) if schema is not None else hit)
            payload = self.payload(m, prompt, system)
            sd = schema_dict(schema)
            if sd is not None:
                payload["format"] = sd
            if sd is None:
                text = self._call(payload)
                result: str | dict = text
                stored = text
            else:
                result, stored = self._generate_validated(payload, schema)
            if use_cache:
                cache_put(key, m, purpose, stored, self.db_path)
            return done(result)
        except Exception as e:
            done(None, ok=False, error=f"{type(e).__name__}: {e}"[:300])
            raise

    def _generate_validated(self, payload: dict, schema) -> tuple[dict, str]:
        base_prompt = payload["prompt"]
        err = ""
        for attempt in range(MAX_RETRIES + 1):
            if err:
                payload["prompt"] = (f"{base_prompt}\n\nYour previous reply was invalid: {err}\n"
                                     "Reply again with valid JSON matching the schema only.")
            raw = self._call(payload)
            try:
                data = json.loads(raw)
                if isinstance(schema, type) and issubclass(schema, BaseModel):
                    data = schema.model_validate(data).model_dump()
                return data, json.dumps(data, ensure_ascii=False)
            except (json.JSONDecodeError, ValidationError) as e:
                err = str(e)[:500]
        raise ValueError(f"LLM output failed schema validation after {MAX_RETRIES + 1} attempts: {err}")


def get_provider(name: str | None = None, **kwargs) -> Provider:
    """Provider selected by name or settings.llm_provider (ollama|none). Cloud names raise ValueError."""
    n = (name or settings.llm_provider or "ollama").lower()
    if n == "none":
        return NullProvider()
    if n == "ollama":
        return OllamaProvider(**kwargs)
    raise ValueError(f"unknown LLM provider {n!r} (expected ollama|none; cloud providers disabled by policy)")


def ollama_status(base_url: str | None = None, timeout: float = 3) -> dict:
    """Reachability + installed models via GET /api/tags."""
    url = (base_url or settings.ollama_base_url).rstrip("/")
    try:
        r = requests.get(f"{url}/api/tags", timeout=timeout)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        return {"reachable": True, "base_url": url, "models": models}
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "base_url": url, "models": [], "error": str(e)[:200]}
