# Tonight report (checkpoints 1-6, no LLM)

> **Superseded: see `docs/FINAL_REPORT.md`.** This is the historical end-of-day-1 report. All LLM TODO items below are now DONE (OllamaProvider, grounded Q&A, LLM extraction, component reports, document summary, UI model selector, evaluation under `eval/`).


## What works
- Download + inspection of 5 real AUTOSAR PDFs; controlled ingestion (reject draft/non-PDF/duplicates, audit log), section-aware chunking, per-project Chroma index (bge-small-en-v1.5; hashing fallback).
- Deterministic extraction (table + heading/sentence heuristics) into SQLite, dependency graph (networkx + pyvis HTML/JSON).
- Eight rule checks (M1-M4, C1-C4), revision comparison with rename detection, csv/xlsx/json export with review_status + citations, review workflow with roles.
- Grounded Q&A pipeline (retrieval, threshold refusal, labelled prompt, citation parsing, confidence rule) - evidence-only mode without an LLM.
- Component reports, flow summaries, document summary stub; FastAPI service (X-User auth) and Streamlit UI (requests only).
- Test suite: 85 passed, 1 deselected (llm marker).

## Real-data extraction counts (components / ports / interfaces / signals)
| doc | counts | graph nodes/edges |
|---|---|---|
| bodycomfort_r3.1.5 and r3.2.3 | 29 / 0 / 0 / 26 | 55 / 0 |
| chassis_r3.1.5 and r3.2.3 | 10 / 0 / 0 / 0 | 10 / 0 |
| hmi_r25-11 | 5 / 0 / 5 / 0 | 10 / 0 |

Real findings: BodyComfort 22 x C4 per revision; Chassis 4 x C4 + 1 x M4; HMI 5 x C4 + 1 x M4 (all labelled "potential, needs review"). Real revision diffs R3.1.5 -> R3.2.3 (BodyComfort, Chassis): 0/0/0/0 entity changes, change history 2->3 and 1->3 rows.

## Evaluation (re-run at the end)
- eval_checks: syn_hld_v1 found 8/8, missed none, 0 false positives; syn_hld_v2 found 5/5 remaining defects, 0 false positives.
- eval_diff (synthetic v1 -> v2): TP=9 FN=0 FP=0, accuracy 100% (target >= 90%).
- eval_extraction: P=R=1.00 and field accuracy 1.00 for components, ports, interfaces, signals on v1 (7/17/8/6) and v2 (8/17/8/6).

## Smoke test (real DB)
uvicorn: /health -> ok, llm_mode none; /projects as admin -> 4 projects; /ask "What does the FrontWasher component do?" on body-comfort -> evidence_only, Low, 5 cited chunks (top: Wiper/Washer section 4.1, p8-10, score 0.77); synthetic component report for WinCtrlManager -> ports/interfaces/upstream/downstream cited, purpose "LLM not configured"; viewer on /audit -> 403. Streamlit started headless and answered its health check; py_compile of the UI passes.

## Known weak spots
- Real PDFs lack port/interface tables: 0 ports, 0 edges, so M1/M2/M3/C2/C3 and graph-based reports are empty on real data.
- Signal page is the start page of the containing section (can be earlier than the page where the label is drawn); signal sender/receivers unknown.
- C4 (missing description) dominates the real findings; some components are coarse group headings.
- Hashing embedder fallback exists; real runs used bge. Hash scores are not comparable with the 0.35 threshold tuned for bge.
- The synthetic evaluation was built to match the parser, so its 100% is a clean-fit upper bound.
- Upload endpoint runs extraction but not checks; re-running extraction resets review decisions for that document.
- Pilot auth is a plain header; role is global per user, membership per project.
- Excel/CSV export via the API covers inventory and findings only (diff export is available in app/export.py, not exposed over HTTP).

## LLM TODO list (all DONE, see FINAL_REPORT.md)
1. [DONE] `app/llm.py` `OllamaProvider.generate`: POST `{base_url}/api/generate` (model, prompt, system, stream false, temperature 0, `format` = JSON schema when `schema` is given); parse `response`; map connection errors to `LLMNotConfigured`. Set `LLM_PROVIDER=ollama` and `OLLAMA_MODEL=qwen2.5:3b` (or `llama3.2:3b`) in `.env`; local models only, no API keys; fill the `@pytest.mark.llm` test in `tests/test_llm.py`.
2. [DONE] `app/qa.py` `_answer`: the `provider.generate(prompt, system)` call is already wired; verify prompt wording on real chunks, tune `HIGH_SCORE`, check that citation parsing and confidence behave with real output.
3. [DONE] `app/extract.py` `extract_from_prose`: currently calls `provider.complete` and returns [] without an LLM; switch to `generate` with a schema, extend beyond components to ports/interfaces/signals, set `extraction_method="llm"`, wire `extract_entities(use_llm=True)`.
4. [DONE] `app/summarize.py` `component_report` (purpose paragraph, via `_llm_text`): constrain the prompt to the structured facts and cite them.
5. [DONE] `app/summarize.py` `document_summary`: feed section text/headings, not just titles; add citations per claim.
6. [DONE] UI: the LLM mode selector in `ui/streamlit_app.py` is display only; pass the choice to the API (per-request provider) once providers work.
7. [DONE] Evaluate Q&A answer quality and add a small question set under `eval/`.
