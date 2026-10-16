# Demo scripts

Three scripts for the TechPulse assessment, one fallback plan and one checklist:

- Part A: recorded video, 8 to 10 minutes (target 9:30).
- Part B: live demo, 10 minutes.
- Part C: fallback plan if Ollama, the network or the GPU fails.
- Part D: pre-demo checklist.
- Appendix: the original 4-minute script (still valid as a short version).

Numbers quoted below come from `docs/DEMO_CHECK.md` (a run on 2026-10-07 against the real database) and `eval/results.md`.
Counts of entities, findings or scores can change after a re-ingest or re-evaluation, so look at the screen on the day
and say what you see. Where a figure from the final evaluation belongs, it is marked **[AUTHOR]**.
Questions are in code format; rehearse each once, because a 3B model can phrase answers differently on each run.

## Part A. Recorded video (8 to 10 minutes)

Recording setup: one browser window with the UI (http://localhost:8000, served by the API itself), a second small terminal
visible only when needed, screen recorder with microphone. Top bar: User = `admin`, status dot green ("ready"); there is one
model (`qwen2.5:3b`), so there is no model selector. The UI has four screens: Documents, Ask, Architecture, Review. The audit
log has no screen: show it with `curl -H "X-User: admin" http://localhost:8000/audit` (or the Swagger page `/docs`).
Start with `/health` showing `warmup: ready` (see Part D). Say your name and the
project title in the first sentence if the guide requires it. **[AUTHOR]** confirm the required video length and format in
the guide.

| Time | Segment | What to click | What to say | What it proves |
|---|---|---|---|---|
| 0:00-0:45 | 1. Problem and design | Show the architecture diagram (README or report section 3). | "AUTOSAR design documents are long PDFs. Reviewers search them by hand and miss inconsistencies. This pilot reads approved documents locally, cites a page for every output, and leaves every output unreviewed until a person decides. No cloud service, no API key." | problem, users, local-only claim |
| 0:45-1:30 | 2. Local and controlled | Terminal: `curl http://localhost:8000/health` (or open it in the browser): show `llm_mode: ollama`, models, `embedder: bge`, `warmup: ready`. Then the Documents screen: choose any PDF, Source status = `draft`, "Upload and process". The rejection reason appears under the form. In the terminal run `curl -H "X-User: admin" http://localhost:8000/audit` and point at the `ingest_rejected` row. | "Models run in Ollama on this laptop. Only approved or synthetic documents are accepted; this draft is rejected and the rejection is logged." | mandatory controls: approved-only ingestion, audit log, local models |
| 1:30-2:30 | 3. Cited answer | Project `body-comfort`, Ask screen: type `Which ports does the FrontWasher component have?` and press Enter. Point at the confidence label, "Unreviewed AI output", the model name and the citation tags `[1 p.8 §4.1]`; click one to jump to the source list (document, version, pages, section, snippet). | "Every sentence ends with a chunk label that maps to a page. Confidence is computed from rules: retrieval score, number of distinct sources and share of cited sentences. The model never grades itself." | grounded Q&A, citations, rule-based confidence |
| 2:30-3:15 | 4. Refusal and isolation | Ask `What is the maximum torque of the diesel engine?` Show the refusal block ("Not found in the approved documents", grey, no sources) and its limitation text with the score and the 0.65 threshold. Switch project to `chassis` and repeat the FrontWasher question: refused, no citations. | "Below the similarity threshold the system refuses instead of guessing. Each project has its own index, so another project's documents cannot be retrieved." | refusal threshold, project isolation |
| 3:15-4:30 | 5. Inventory and diagram extraction | Project `body-comfort`, Architecture screen, document `bodycomfort_r3.2.3`, Ports tab: type `diagram` in the filter box to show ports with method `diagram` and their page tags. Say that these documents draw ports as vector graphics, not tables. Optionally show a debug render from `docs/diagram_checks/`. | "Ports in these documents are drawn as boxes and labels. The diagram extractor reads the PDF's drawing geometry: boxes are components, labels near a border are ports, labels on arrows are signals. Every entity lists all its sources." | innovation: geometry extraction; method and confidence per entity |
| 4:30-5:15 | 6. Graph | Project `synthetic-ecu`, Architecture screen, document `syn_hld_v1`, Dependency graph: Focus = `AntiPinchMonitor`, Depth 1. Hover a node to show its citation. | "The graph is built from the stored inventory. Component links are provider to consumer." | dependency mapping |
| 5:15-6:00 | 7. Reports | Still on Architecture: click `WinCtrlManager` in the Components table. The side panel shows the component report (fact rows with page tags, cited purpose paragraph, confidence, limitation) and the flow steps that name it. Esc closes it; then "Functional-flow summary" for the whole document. | "Facts come from the inventory; the purpose paragraph is written by the model from retrieved text and sentences without a valid citation are removed." | cited summaries and reports |
| 6:00-6:45 | 8. Consistency checks | Review screen, `syn_hld_v1`: show the 8 findings grouped by rule (M1-M4, C1-C4) with severity and page tags. Say that on the real documents findings are labelled potential. | "The checks are plain Python rules over the inventory, not model opinions, so each finding points at the entities and pages that caused it. Eight defects were planted in this document and eight are found, with no false positive." | rule-based checks, explainability |
| 6:45-7:45 | 9. Revision comparison | Review screen, Compare versions: `syn_hld_v1` vs `syn_hld_v2`: entity changes (components +1/~1, ports +1/-1/~3 per DEMO_CHECK). Then project `body-comfort`, `bodycomfort_r3.1.5` vs `bodycomfort_r3.2.3`: expand a row under Section changes and open "Change-history tables". | "The entity diff finds the nine planted changes. Between the two real releases the entities are identical, so the section diff and the change-history table are what shows what moved." | entity diff and section diff |
| 7:45-8:15 | 10. OCR | Project `synthetic-ecu` (document `syn_scanned_sample` is pre-ingested), Ask screen: `Which component provides the ISwitchCmd interface and what are the steps of manual window operation?` Point at the "from OCR, confidence N" note in the limitation. | "This document has no text layer. Pages are rendered and read with Tesseract, and the confidence travels into the citation so the reader knows." | OCR with provenance |
| 8:15-9:00 | 11. Human review and export | Review screen. Switch the top-bar user to `viewer`: the Approve / Reject buttons are not there at all (and the API returns 403 if called directly). Switch to `reviewer`: approve one finding, reject another; the status label changes in place. In the terminal, `/audit` as admin shows both `review` rows. Export format `xlsx`, "Findings": open the file: banner row, `review_status`, `doc_id`, `version`, `page`, `section`. | "Only reviewers can decide, every decision is logged, and exports carry the status and the citation." | human oversight, roles, audit, structured export |
| 9:00-9:40 | 12. Evaluation and limits | Open `eval/results.md` (the UI has no evaluation screen): show the Q&A table and the extraction table including the held-out v4 row. | Read the numbers from the screen: **[AUTHOR]** the final Q&A correctness, citation accuracy, refusal accuracy, v4 held-out F1 and defect count. "The v1 score is an upper bound because that document fits the parser. v4 was frozen before the parser changes and scored once. Real-document precision is not reported because the gold file is not human-verified. Review effort was **[AUTHOR]** measured at X / not measured." | honest evaluation, limitations |
| 9:40-10:00 | 13. Close | Back to the architecture slide. | "What it does: cited, reviewable, local. What it does not do: it does not approve designs, its real-document extraction is unverified, and authentication is a pilot header." Name the next steps from section 9 of the report. | limitations and next steps |

Time guard: if running long, cut segment 6 (graph) and shorten 10 (OCR) to a screenshot. If short, show the source list under the answer
(click a citation tag to jump to its source snippet) in segment 3.

## Part B. Live demo (10 minutes)

Same flow, fewer clicks, answers to questions at the end. Use `qwen2.5:3b` (about 3 s per answer in the evaluation) and
questions already answered once, so the response cache replays them instantly if the model is slow.

| Time | What | Notes |
|---|---|---|
| 0:00-1:00 | Problem, design in three sentences, show `/health` ready | If the status dot says "warming", talk through the design until it turns green |
| 1:00-2:00 | Documents: rejected draft upload; audit row via `curl /audit` | proves controls |
| 2:00-4:00 | Ask: cited answer (`body-comfort`), refusal, isolation via `chassis` | the core of the assistant |
| 4:00-5:30 | Architecture: diagram-sourced ports, then graph and `WinCtrlManager` side panel on `synthetic-ecu` | innovation: geometry extraction |
| 5:30-6:30 | Review: 8 planted defects with rule, severity and page | explainable checks |
| 6:30-7:30 | Review, Compare versions: entity diff and section diff | show the section diff on the real releases |
| 7:30-8:30 | Roles: viewer sees no review buttons, reviewer approves, admin audit via `/audit`; Excel export | human oversight |
| 8:30-9:00 | Evaluation numbers and limits (read from `eval/results.md`) | same honest statements as the video |
| 9:00-10:00 | Questions | see the likely questions in `docs/CODE_WALKTHROUGH.md` |

Skip candidates if time is short: OCR (mention it and show the Q&A on `syn_scanned_sample` only if asked), graph, Excel export.
Do not run LLM extraction or upload an approved PDF live (upload and extraction take about 26 s for BodyComfort without LLM
extraction, and much longer with it); show the finished result.

## Part C. Fallback plan

| Failure | Detection | What to do | Segments to use |
|---|---|---|---|
| Ollama not running or the model is still loading | `/health` shows `ollama.reachable: false` or `warmup: degraded` (the top-bar status dot turns red; hover it for the reason); the first answer takes 30 to 50 s | Start Ollama (`ollama serve`), wait for `warmup: ready`; meanwhile continue with the parts that need no LLM: Architecture (inventory, graph), Review (findings, compare, export), `/audit` | video segments 2, 5, 6, 8, 9, 11 |
| The model is slow or answers badly | answer takes more than 15 s, or the wording is poor | rely on cached answers (the response cache returns identical text instantly for a repeated prompt: ask the rehearsed questions first); there is no second model to switch to, so use the recorded clip | video segment 3 clip as backup |
| Ollama or the GPU cannot be fixed | no `ollama.reachable` after a restart | restart the API with `LLM_PROVIDER=none` (`$env:LLM_PROVIDER='none'` then start `uvicorn app.api:app`): Q&A returns the top retrieved passages with the notice "LLM not configured - showing top retrieved evidence" and confidence Low (evidence-only mode); reports show structured facts only. Say so explicitly: "this is the degraded mode by design" | video segments 3 and 7 for the full answers |
| No network | none needed after the first model download, because everything is local; `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` avoid Hugging Face lookups | export those two variables before starting the API; the evaluation's `offline_run_works` control used them | n/a |
| Embedder fails to load | `/health` shows `embedder.fallback: true` | Q&A refuses by design (thresholds are only valid for bge); fix the cache or skip Q&A and use the video clip | video segments 3 and 4 |
| Database or index corrupted on the day | missing documents in the selectors | rebuild into a scratch location with `python scripts/demo_reset.py --yes --data-dir D --db-path P --chroma-dir C --processed-dir R --raw-dir W [--keep-llm-cache]`; never point it at the main `data/hld.db` (it holds the LLM response cache) | n/a |
| Screen recording or UI problem | n/a | reload the page (all state is on the server); otherwise use the API directly (`/docs` Swagger page at http://localhost:8000/docs, header `X-User: admin`) | n/a |

Keep the recorded video and a copy of the evaluation outputs on the same laptop. In the live demo say "I have a recording of this
step" and jump to the matching segment: Q&A = A3, refusal and isolation = A4, diagram extraction = A5, checks = A8, comparison = A9,
OCR = A10, review and export = A11. **[AUTHOR]** after recording, replace the segment names with the real timestamps in the file.

## Part D. Pre-demo checklist

Do this 30 minutes before, in this order:

1. Ollama running: `ollama list` shows `qwen2.5:3b`. Close other GPU programs (the evaluation used 3.2 of 4 GB).
2. Reset to a clean state in a scratch target, not the main database:
   `python scripts/demo_reset.py --yes --data-dir <D> --db-path <P> --chroma-dir <C> --processed-dir <R> --raw-dir <W> --keep-llm-cache`
   (rebuilds seed, ingest of real, synthetic and held-out documents, extraction, checks, diffs and reports and prints per-step timings;
   without `--yes` it deletes nothing). If you demo from the main data, do not reset; just verify.
3. Ingest the scanned sample for segment 10: `python scripts/ingest_scanned.py` (leaves an existing document untouched).
4. Start the API with the same environment variables as the target above: `python -m uvicorn app.api:app --port 8000`.
5. Check `http://localhost:8000/health`: `status: ok`, `warmup.status: ready` (it reads `warming` for the first seconds and `degraded`
   with a reason if the embedder or Ollama failed), `ollama.reachable: true`, `embedder.kind: bge`, `demo_model: qwen2.5:3b`.
6. Open http://localhost:8000 (the API serves the UI; there is no second process). The status dot in the top bar is green ("ready"), not "warming" or red.
7. Ask each demo question once (this also fills the response cache): FrontWasher ports, the diesel torque refusal, the scanned-sample question, the
   `chassis` FrontWasher refusal, a `component_report` for `WinCtrlManager`.
8. Keep a draft PDF (any PDF) on the desktop for the rejected upload.
9. Open in tabs: the architecture diagram, `eval/results.md`, `docs/DEMO.md`, the pre-recorded video, the Swagger page.
10. Disable notifications; set browser zoom so tables are readable; check microphone level.
11. Fallback: have `LLM_PROVIDER=none` command ready in a terminal history.
12. **[AUTHOR]** run `python scripts/demo_check.py` (see `docs/DEMO_CHECK.md`) against the same API if you want an automated pass over the 11 scenarios.

Questions verified in `docs/DEMO_CHECK.md` and `eval/gold_questions.json` (answer correctness varies by model, see `eval/results.md`):
`chassis` "What does the ACC software composition output?"; `body-comfort` "What do the naming prefixes Cmd, Req and Sta mean in data
element names?"; `synthetic-ecu` "Which component drives the window lift motor?"; `synthetic-ecu` "Who sends the MotorCommand signal and who
receives it?".

---

## Appendix: original 4-minute script

Setup before recording: the API running (`uvicorn app.api:app --port 8000`; it serves the UI at http://localhost:8000), Ollama running, seeded DB with the real documents and synthetic v1/v2 ingested, bge
model cached (the first Q&A after start takes 30 to 50 s while it loads unless the warm-up has finished; wait for `/health` `warmup: ready`).
Keep a draft PDF (any PDF) ready for scenario 1.

Questions below are the ones exercised in `docs/DEMO_CHECK.md` (all 11 scenarios passed through the API; the UI is covered by page tests and a Playwright smoke test, and
`scripts/screenshots.py` walks the four screens in Chromium).

| Time | # | Scenario | What to do and say |
|---|---|---|---|
| 0:00 | - | Intro (15 s) | "Local assistant for AUTOSAR HLDs: no cloud, every output cited and unreviewed until a human decides." Show user selector = admin, project `body-comfort`. |
| 0:15 | 1 | Controlled ingestion | Documents: choose any PDF, source status `draft`. Rejected inline with "source_status 'draft' not allowed"; `curl -H "X-User: admin" localhost:8000/audit` shows the `ingest_rejected` row. |
| 0:35 | 2 | Cited answer | Ask, project `body-comfort`: **"Which ports does the FrontWasher component have?"** Show the Medium label, model name, citation tags (p.9 s4.1, p.11 s4.1.1). Mention the upload of the approved PDF takes about 26 s (show the finished result, not the wait). |
| 1:00 | 3 | Refusal | **"What is the maximum torque of the diesel engine?"** Refused, Low, limitation "Best retrieval score 0.54 is below the threshold 0.65". |
| 1:15 | 4 | Inventory and graph | Switch to `synthetic-ecu`, doc `syn_hld_v1`. Architecture: tabs show 7 components, 17 ports, 8 interfaces, 6 signals; graph 39 nodes / 53 edges; Focus `AntiPinchMonitor`, depth 1. |
| 1:50 | 5 | Reports | Architecture: click `WinCtrlManager` for the side-panel report (every fact line cited), then "Functional-flow summary" (13 steps, all cited). |
| 2:20 | 6 | Checks | Review: 8 findings = 8 planted defects, each with page evidence, 0 false positives. |
| 2:45 | 7 | Roles and review | Review screen, user `viewer`: no Approve / Reject buttons (the API refuses with 403 anyway). Switch to `reviewer`: approve one finding, reject another. `/audit` as admin shows both `review` actions. |
| 3:10 | 8 | Compare | Review, Compare versions: `syn_hld_v1` vs `syn_hld_v2`: components +1/~1, ports +1/-1/~3, total added 2, removed 1, changed 6; change history 1 vs 2 rows. |
| 3:25 | 9 | Project isolation | Switch to `chassis`, ask **"Which ports does the FrontWasher component have?"** Refused, no citations. |
| 3:35 | 10 | Offline | Say the same question worked with network access blocked at socket level (tested, not unplugged); if live, disable Wi-Fi and repeat a `body-comfort` question. |
| 3:45 | 11 | Export | Review, export format xlsx, "Findings"; open it to show `review_status`, `doc_id`, `version`, `page`, `section` columns and the "Contains unreviewed AI output" banner row. |
| 3:55 | - | Close | State limitations in one sentence: real gold not yet human-verified, held-out result as in `eval/results.md`. |
