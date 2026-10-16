"""Start the portable local runtime if necessary; never change an existing Ollama service."""
from __future__ import annotations

import os
import subprocess
import time
from urllib.parse import urlsplit

import requests

from app.config import settings


def main() -> int:
    if settings.llm_provider != "ollama":
        return 0
    base = settings.ollama_base_url.rstrip("/")
    def alive():
        try:
            return requests.get(base + "/api/version", timeout=2).ok
        except requests.RequestException:
            return False
    if alive():
        return 0
    address = urlsplit(base)
    executable = settings.project_root / "tools/ollama/ollama.exe"
    if address.hostname not in ("localhost", "127.0.0.1") or not executable.exists():
        print("Start your configured Ollama service before launching full mode.")
        return 1
    logs = settings.data_dir / "server-logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "OLLAMA_HOST": f"127.0.0.1:{address.port or 11434}",
           "OLLAMA_MODELS": str(settings.data_dir / "models/ollama"), "OLLAMA_VULKAN": "1",
           "OLLAMA_CONTEXT_LENGTH": "4096", "OLLAMA_NUM_PARALLEL": "1"}
    with (logs / "ollama.log").open("a", encoding="utf-8") as output:
        process = subprocess.Popen([str(executable), "serve"], env=env, stdout=output, stderr=output,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    for _ in range(30):
        if alive():
            print("Local Ollama runtime started.")
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.5)
    print(f"Ollama did not start. Check {logs / 'ollama.log'}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
