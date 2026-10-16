"""Readiness checks, without silently substituting a working answer model."""
from __future__ import annotations

import json
import os

from app import auth, llm, retrieve
from app.config import settings


def main() -> int:
    issues = []
    try:
        mode = "token" if auth.enabled() else "local_demo"
    except Exception as exc:
        issues.append(str(exc))
        mode = "invalid"
    embedder = retrieve.get_embedder()
    status = llm.ollama_status() if settings.llm_provider == "ollama" else {"reachable": False}
    evidence_only = settings.llm_provider == "none" and os.environ.get("EMBEDDER") == "hash"
    if not evidence_only:
        if embedder.name != "bge":
            issues.append("The BGE semantic-search model could not load.")
        if settings.llm_provider != "ollama" or not status.get("reachable"):
            issues.append("Ollama is not available for generated answers.")
        else:
            import requests
            try:
                response = requests.get(settings.ollama_base_url.rstrip("/") + "/api/tags", timeout=5)
                response.raise_for_status()
                models = {x["name"] for x in response.json().get("models", [])}
                for name in (settings.demo_model, settings.llm_extract_model):
                    if name not in models:
                        issues.append(f"Ollama model is missing: {name}")
            except (requests.RequestException, ValueError, KeyError) as exc:
                issues.append(f"Could not list Ollama models: {exc}")
    print(json.dumps({"ready": not issues, "mode": "evidence_only" if evidence_only else "full",
                      "auth": mode, "embedder": embedder.name, "issues": issues}, indent=2))
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
