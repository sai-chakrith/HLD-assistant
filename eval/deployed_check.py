"""End-to-end check of the deployed model split through the real API (uvicorn), with LLM cache reads off:
DEMO_MODEL (Q&A, summaries; warmed at start-up, kept loaded) and LLM_EXTRACT_MODEL (prose extraction).

Steps: unload every model -> start the server on copies of the DB / Chroma / processed data -> wait for the warm-up
-> 25 gold questions -> upload a synthetic HLD with LLM extraction -> the same question asked right after the
extraction (model swap back to the Q&A model) and once more (warm). Writes eval/results.json["deployed_check"],
the "Deployed configuration" section of eval/results.md and lines in eval/run.log.

    .venv\\Scripts\\python -m eval.deployed_check
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.run_eval import (EVAL_DIR, WORK, _ollama, is_refusal, key_fact_hits, lat_stats, log, ms, pct,  # noqa: E402
                           table, trace_entries, unload_all)

SWAP_Q = ("synthetic-ecu", "Which component drives the window lift motor?")
UPLOAD_PDF = ROOT / "data/synthetic/hld_v2.pdf"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def loaded() -> list[str]:
    return [f"{m['name']} ({100 * (m.get('size_vram') or 0) / max(1, m.get('size') or 1):.0f}% GPU)"
            for m in _ollama("/api/ps").get("models", [])]


def main() -> int:
    import httpx
    from app.config import settings
    w = WORK / "deployed"
    shutil.rmtree(w, ignore_errors=True)
    w.mkdir(parents=True)
    shutil.copy(settings.db_path, w / "hld.db")
    shutil.copytree(settings.chroma_dir, w / "chroma")
    shutil.copytree(settings.processed_dir, w / "processed")
    trace = w / "traces.jsonl"
    env = {**os.environ, "DB_PATH": str(w / "hld.db"), "CHROMA_DIR": str(w / "chroma"),
           "PROCESSED_DIR": str(w / "processed"), "TRACE_PATH": str(trace), "LLM_CACHE_READ": "0", "WARMUP": "1",
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONIOENCODING": "utf-8"}
    out: dict = {"generated": time.strftime("%Y-%m-%d %H:%M"), "demo_model": settings.demo_model,
                 "extract_model": settings.llm_extract_model, "keep_alive": settings.demo_keep_alive}
    log(f"\n## deployed check {out['generated']}: Q&A {settings.demo_model} (keep_alive {settings.demo_keep_alive}), "
        f"extraction {settings.llm_extract_model}; LLM cache reads off")
    unload_all()
    port = _port()
    base = f"http://127.0.0.1:{port}"
    t0 = time.perf_counter()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.api:app", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=open(w / "uvicorn.log", "w"), stderr=subprocess.STDOUT)
    H = {"X-User": "admin"}
    try:
        while True:
            try:
                h = httpx.get(base + "/health", timeout=5).json()
                if h["warmup"]["status"] != "warming":
                    break
            except httpx.HTTPError:
                pass
            if time.perf_counter() - t0 > 300 or proc.poll() is not None:
                raise SystemExit("server/warm-up did not finish")
            time.sleep(0.5)
        out["startup_to_ready_ms"] = (time.perf_counter() - t0) * 1000
        out["warmup"] = h["warmup"]
        out["loaded_after_warmup"] = loaded()
        log(f"server ready + warm-up {out['startup_to_ready_ms'] / 1000:.1f}s {h['warmup']}; loaded: "
            f"{out['loaded_after_warmup']}")
        # 25 gold questions
        qs = json.loads((EVAL_DIR / "gold_questions.json").read_text(encoding="utf-8"))["questions"]
        rows = []
        for q in qs:
            t = time.perf_counter()
            a = httpx.post(f"{base}/projects/{q['project_id']}/ask", headers=H, json={"question": q["question"]},
                           timeout=600).json()
            dt = (time.perf_counter() - t) * 1000
            ref = is_refusal(a["answer"], a["mode"])
            ok = ref if q["category"] == "unanswerable" else (not ref and key_fact_hits(a["answer"], q["key_facts"])
                                                               [0] == len(q["key_facts"]))
            rows.append({"id": q["id"], "category": q["category"], "ms": dt, "correct": ok, "model": a.get("model"),
                         "mode": a["mode"], "confidence": a["confidence"]})
            print(f"   {q['id']} {a['mode']:9} {a.get('model') or '-':18} ok={ok} {dt / 1000:.1f}s", flush=True)
        ans = [r for r in rows if r["category"] != "unanswerable"]
        un = [r for r in rows if r["category"] == "unanswerable"]
        out["gold"] = {"rows": rows, "correctness": sum(r["correct"] for r in ans) / len(ans),
                       "refusal_accuracy": sum(r["correct"] for r in un) / len(un),
                       "latency": lat_stats([r["ms"] for r in rows]),
                       "latency_generated": lat_stats([r["ms"] for r in rows if r["mode"] == "generated"]),
                       "models_used": sorted({r["model"] for r in rows if r["model"]})}
        out["loaded_after_gold"] = loaded()
        g = out["gold"]
        log(f"gold Q&A: correct {g['correctness']:.2f}, refusal {g['refusal_accuracy']:.2f}, latency p50/p95 "
            f"{g['latency']['p50_ms'] / 1000:.1f}/{g['latency']['p95_ms'] / 1000:.1f}s, models {g['models_used']}; "
            f"loaded: {out['loaded_after_gold']}")
        # upload + LLM extraction (extraction model)
        pdf = w / "deployed_check_hld.pdf"  # same content, unique bytes (a comment after %%EOF): new sha256
        pdf.write_bytes(UPLOAD_PDF.read_bytes() + f"\n%deployed-check {time.time()}\n".encode())
        t = time.perf_counter()
        with open(pdf, "rb") as f:
            r = httpx.post(f"{base}/projects/synthetic-ecu/upload", headers=H, timeout=1800,
                           files={"file": ("deployed_check_hld.pdf", f, "application/pdf")},
                           data={"doc_type": "hld", "version": "9.1", "source_status": "synthetic", "use_llm": "true"})
        out["upload"] = {"status": r.status_code, "ms": (time.perf_counter() - t) * 1000,
                         "extracted": r.json().get("extracted") if r.status_code == 200 else r.text[:300],
                         "loaded_after": loaded()}
        if r.status_code != 200:
            raise SystemExit(f"upload failed: HTTP {r.status_code} {r.text[:300]}")
        ext = [e for e in trace_entries(trace) if e.get("purpose") == "extract_prose"]
        out["upload"]["extract_calls"] = {"n": len(ext), "models": sorted({e["model"] for e in ext}),
                                          "first_call_ms": ext[0]["latency_ms"] if ext else None,
                                          "rest": lat_stats([e["latency_ms"] for e in ext[1:]])}
        log(f"upload + extraction: HTTP {r.status_code} {out['upload']['ms'] / 1000:.1f}s, extracted "
            f"{out['upload']['extracted']}, calls {out['upload']['extract_calls']['n']} by "
            f"{out['upload']['extract_calls']['models']} (first call {out['upload']['extract_calls']['first_call_ms']} ms "
            f"incl. load); loaded: {out['upload']['loaded_after']}")
        # ask right after the extraction (swap back), then again (warm)
        swap = []
        for label in ("after extraction (swap back)", "repeat (warm)"):
            t = time.perf_counter()
            a = httpx.post(f"{base}/projects/{SWAP_Q[0]}/ask", headers=H, json={"question": SWAP_Q[1]},
                           timeout=600).json()
            swap.append({"label": label, "ms": (time.perf_counter() - t) * 1000, "model": a.get("model"),
                         "mode": a["mode"], "loaded": loaded()})
        out["swap"] = swap
        out["swap_delay_ms"] = swap[0]["ms"] - swap[1]["ms"]
        log(f"ask after extraction {swap[0]['ms'] / 1000:.1f}s vs warm {swap[1]['ms'] / 1000:.1f}s -> swap delay "
            f"~{out['swap_delay_ms'] / 1000:.1f}s; loaded after: {swap[1]['loaded']}")
        qa_tr = [e for e in trace_entries(trace) if e.get("purpose") in ("qa", "qa_desc")]
        out["qa_generation_calls"] = {"models": sorted({e["model"] for e in qa_tr}),
                                      **lat_stats([e["latency_ms"] for e in qa_tr])}
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    R = json.loads((EVAL_DIR / "results.json").read_text(encoding="utf-8"))
    R["deployed_check"] = out
    (EVAL_DIR / "results.json").write_text(json.dumps(R, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    md = (EVAL_DIR / "results.md").read_text(encoding="utf-8")
    md = md.split("\n## Deployed configuration", 1)[0].rstrip() + "\n\n" + "\n".join(render(out, R)) + "\n"
    (EVAL_DIR / "results.md").write_text(md, encoding="utf-8")
    return 0


def render(o: dict, R: dict) -> list[str]:
    g, u, s = o["gold"], o["upload"], o["swap"]
    d = R.get("decision", {})
    return [
        "## Deployed configuration (author's post-hoc choice)", "",
        f"The pre-set decision rule above favoured **{d.get('default', 'qwen2.5:3b')}** as the single default "
        f"(failed: {', '.join(d.get('failed') or []) or '-'}: qwen3:4b-instruct runs only partly on the 4 GB GPU). "
        "After seeing the results the author chose a split instead, and this choice was made post hoc, not by the "
        "rule:",
        f"- Q&A, component reports and document summaries: **{o['demo_model']}** (DEMO_MODEL), warmed at start-up "
        f"and kept loaded for {o['keep_alive']} (DEMO_KEEP_ALIVE): +20 points gold Q&A correctness in block C.",
        f"- LLM prose extraction: **{o['extract_model']}** (LLM_EXTRACT_MODEL): equal or better extraction F1, "
        "fewer defect false positives and 5x faster per section in blocks A, B and F.",
        "", f"### End-to-end check of this setup ({o['generated']}; real API server, LLM cache reads off)", "",
        table(["step", "result"], [
            ["server start + warm-up (embedder + 1-token Q&A model prompt)",
             f"{ms(o['startup_to_ready_ms'])}; loaded: {', '.join(o['loaded_after_warmup']) or 'none'}"],
            ["25 gold questions: correctness / refusal accuracy",
             f"{pct(g['correctness'])} / {pct(g['refusal_accuracy'])} (models used: {', '.join(g['models_used'])})"],
            ["25 gold questions: end-to-end latency p50 / p95 (all / generated answers)",
             f"{ms(g['latency']['p50_ms'])} / {ms(g['latency']['p95_ms'])}; "
             f"{ms(g['latency_generated']['p50_ms'])} / {ms(g['latency_generated']['p95_ms'])}"],
            ["upload + ingest + LLM extraction + checks (synthetic HLD, 'deployed_check_hld')",
             f"HTTP {u['status']}, {ms(u['ms'])}; extracted {u['extracted']}"],
            ["extraction calls", f"{u['extract_calls']['n']} by {', '.join(u['extract_calls']['models']) or '-'}; first "
                                 f"call {ms(u['extract_calls']['first_call_ms'])} (includes loading the extraction "
                                 f"model), others p50 {ms(u['extract_calls']['rest']['p50_ms'])}"],
            ["loaded after the extraction", ", ".join(u["loaded_after"]) or "none"],
            [f"ask right after the extraction ({s[0]['model']})", f"{ms(s[0]['ms'])}"],
            ["same ask again (warm)", f"{ms(s[1]['ms'])}"],
            ["model-swap delay (first ask after extraction - warm ask)", ms(o["swap_delay_ms"])],
            ["loaded at the end", ", ".join(s[1]["loaded"]) or "none"],
        ]), ""]


if __name__ == "__main__":
    sys.exit(main())
