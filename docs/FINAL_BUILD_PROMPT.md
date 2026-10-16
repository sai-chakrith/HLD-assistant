# Final Build Prompt: Complete the Project + Full Evaluation

Paste everything inside the code block into your coding agent, opened in `hld_assistant/`. Start Ollama first and check that `ollama list` shows `qwen2.5:3b` and `llama3.2:3b`.

````
You are completing "AUTOSAR HLD Document Analysis Assistant".
Read these first, in order: docs/TONIGHT_REPORT.md, docs/data_notes.md, docs/project.md, docs/scope.md, docs/outcomes.md.
TONIGHT_REPORT.md says what already works (85 tests passing) and what is weak. Do not rebuild working parts.

=====================================================================
HARD RULES
=====================================================================
- LOCAL MODELS ONLY. Ollama at OLLAMA_BASE_URL (default http://localhost:11434). Models: qwen2.5:3b (default)
  and llama3.2:3b (comparison). No cloud LLM APIs, no API keys, no Groq/OpenAI/Langfuse cloud. After models
  and the embedder are downloaded, everything must work with the internet off.
- 4 GB VRAM: one LLM call at a time, num_ctx 4096, keep prompts small (one section or a few chunks per call).
- Cache every LLM call in SQLite (key = sha256 of model + system + prompt + schema). Eval reruns must be cheap.
- The LLM extracts, answers and summarizes. Checks stay deterministic Python rules.
- Every output keeps a citation (doc_id, version, page, section), confidence, and review_status starting "unreviewed".
- Project isolation and roles stay enforced everywhere.
- Windows: run with .venv\Scripts\python, set PYTHONIOENCODING=utf-8, import PyMuPDF as `import pymupdf`.
- Keep the full test suite green after each phase. New LLM tests use @pytest.mark.llm.
- Commit to git after each phase and print "PHASE X DONE" with a 3-line summary.
- REPORT HONESTLY. Never tune the evaluation to the parser or inflate numbers. If something fails, say so in the report.

=====================================================================
PHASE A: Local LLM client
=====================================================================
1. app/llm.py OllamaProvider.generate:
   POST {base_url}/api/generate with model, prompt, system, stream=false, options {temperature: 0.1, num_ctx: 4096},
   and format = JSON schema when a schema is given (pydantic model_json_schema()).
   With a schema: parse JSON, validate with pydantic, retry up to 2 times feeding the validation error back.
   Map connection errors and timeouts to LLMNotConfigured. Model from OLLAMA_MODEL, overridable per call.
2. LLM cache table (see rules) and a local trace log data/traces.jsonl:
   ts, model, purpose, prompt_hash, chunk_ids, latency_ms, cached, ok/error.
3. Per-request model choice: API accepts an optional model field (qwen2.5:3b | llama3.2:3b), anything else is rejected.
   /health reports Ollama reachability and the installed models (GET /api/tags).
4. Fill the @pytest.mark.llm tests (text generation, JSON with schema, cache hit on second call).

=====================================================================
PHASE B: Grounded Q&A with the real LLM
=====================================================================
1. app/qa.py: run generation on real chunks. Prompt: answer ONLY from labelled chunks [C1].., cite a label after every
   sentence, say "Not stated in the provided documents" for missing parts, no outside knowledge.
2. Post-process: drop sentences with no valid label, drop invented labels, map labels to page/section citations.
   If nothing valid remains, return the refusal.
3. Retune SIMILARITY_THRESHOLD and HIGH_SCORE on 10 real questions (5 answerable, 5 not). Write the chosen values
   and the reasoning into docs/data_notes.md under "Q&A tuning".
4. Confidence stays rule-based (retrieval score, number of supporting chunks, share of sentences cited). Document it.

=====================================================================
PHASE C: Real-data extraction uplift (most important phase)
=====================================================================
TONIGHT_REPORT says the real PDFs gave 0 ports, 0 edges, and signals with unknown sender/receivers, because ports
only appear as diagram labels. Fix this in three layers. Each entity records extraction_method and a confidence.

C1. Diagram geometry (deterministic, method="diagram"):
    On diagram pages, use pymupdf page.get_drawings() to find rectangles (component boxes) and
    page.get_text("dict") for label spans with bounding boxes. Assign a box's title text as the component.
    Attach port/signal labels (the regex in data_notes.md section 5) to the nearest box whose border they touch or sit
    just outside of (configurable distance). Use lines/arrows from get_drawings() to connect labels between boxes
    where an arrow path links them. Record the exact page where each label is drawn (fixes the "section start page" bug).
    Write a debug script that renders a page PNG with boxes and assigned labels drawn on top
    (scripts/debug_diagram.py <doc_id> <page>) so assignments can be checked visually.

C2. Naming-convention inference (deterministic, method="convention"):
    Use the prefix semantics from data_notes.md (Cmd master->actuator, Req sensor->master, Sta status, Hmi user
    request, Dis display feedback, Err failure feedback) to infer port direction and likely sender/receiver role.
    Mark these as inferred, with confidence "Medium" at most. Never override diagram or table evidence.

C3. LLM prose extraction (method="llm"):
    extract_from_prose: one section at a time, pydantic schema for components, ports, interfaces, signals with an
    evidence quote (<= 25 words) copied from the section. Discard any entity whose evidence quote is not found in the
    section text (anti-hallucination check). Wire extract_entities(use_llm=True).

Merge order and precedence: table > diagram > llm > convention. Keep all sources on merged entities.
Re-extraction must PRESERVE review decisions (match entities by normalized key; keep review_status and comments).
Rerun extraction, graph, checks and diffs for all real documents. Print a before/after table of entity counts and edges
per document and add it to docs/data_notes.md under "Extraction uplift".

=====================================================================
PHASE D: Summaries and reports with the LLM
=====================================================================
1. component_report: the "purpose" paragraph is written by the LLM only from the component's retrieved chunks plus its
   structured facts; each sentence cited; uncited sentences dropped.
2. document_summary: map-reduce over section text (not just titles). Map = 2–3 cited bullet points per section,
   reduce = short overview that keeps the citations.
3. functional_flow_summary for real docs: derive steps from graph paths plus the flow/description sections; each step
   cited; flag steps whose entities are not in the inventory.
4. Store reports with model name, confidence, limitation note, review_status.

=====================================================================
PHASE E: Fix the known weak spots from TONIGHT_REPORT
=====================================================================
- Upload endpoint runs extraction AND checks.
- Re-extraction keeps review decisions (done in C; add a test).
- Roles per project (project_members.role) instead of one global role; update require_role and tests.
- Export endpoints for diffs and reports (CSV/XLSX/JSON) with review_status and citations.
- C4 noise: skip group headings that are not components (heading has no prose or is a container heading); show the
  rule in data_notes.md.
- Hash-embedder fallback: refuse Q&A threshold logic when the fallback is active and show a warning in the UI.

=====================================================================
PHASE F: Evaluation (everything, both local models)
=====================================================================
Build eval/ with these gold sets:

F1. Synthetic v1/v2 (existing ground_truth.json): extraction P/R, defects found/missed/FP, diff accuracy.

F2. HELD-OUT synthetic v3: generate data/synthetic/hld_v3_heldout.pdf + ground truth with a DIFFERENT style than v1/v2:
    different table header wording, ports described in prose paragraphs instead of tables, one port table split
    across pages, and 6 planted defects. This measures generalization, since v1/v2 were built to match the parser.
    Do not change the parser after looking at v3 results; report them as they are.

F3. Real-document gold set: create eval/real_gold_candidates.csv listing ~40 entities from BodyComfort R3.2.3 and
    HMI R25-11 (name, type, component, direction, page) taken from the PDFs, with a column verified=no.
    STOP and print: "Human verification needed: open eval/real_gold_candidates.csv, fix rows, set verified=yes,
    save as eval/real_gold.csv, then type continue." Use only verified rows for real-data scores.
    If eval/real_gold.csv is missing, run everything else and mark the real-data scores as "not verified".

F4. Q&A gold set eval/gold_questions.json, 25 questions:
    12 on real docs (expected doc, page, section, and key facts), 8 on synthetic, 5 unanswerable.

eval/run_eval.py --models qwen2.5:3b llama3.2:3b computes, per model:
- Extraction precision / recall / F1 per entity type: synthetic v1, held-out v3, real verified gold
- Per extraction method contribution (table / diagram / convention / llm)
- Defect detection found / missed / false positives (v1, v3)
- Diff accuracy (v1→v2) and real release diffs (counts, listed)
- Q&A: answer correctness (key-fact match), citation accuracy (cited page/section matches expected),
  groundedness (% sentences with valid citation), refusal accuracy on the 5 unanswerable
- Summary groundedness (% cited sentences) on 5 component reports and 2 document summaries
- Confidence calibration: accuracy for High / Medium / Low answers
- Control tests: unapproved upload rejected, cross-project query empty, viewer cannot approve, offline run works
  (set HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1 and check that ask + extract still run)
- Latency p50 / p95 per query and per section extraction, plus VRAM note
Outputs: eval/results.json, eval/results.md (one comparison table per area, both models side by side),
and 3–4 PNG charts in eval/charts/ (matplotlib): extraction F1 by type and model, Q&A metrics by model,
latency by model, entity counts before/after uplift.
Pick the default model from the results and state why in results.md.

=====================================================================
PHASE G: UI completion
=====================================================================
- Model selector (qwen2.5:3b / llama3.2:3b) actually sent to the API; Ollama status shown in the sidebar.
- Q&A: citation cards (doc, version, page, section, snippet), confidence badge, limitation note, model used.
- Inventory: extraction_method and confidence columns, filter by method.
- Graph: embedded pyvis, filter by component; a "show evidence" link to the page.
- Reports tab: component report, flow summary, document summary.
- Issues: grouped by rule, evidence pages, approve/reject/correct (hidden for viewers), "potential" label on real docs.
- Compare: version picker, added/removed/changed/possible rename, change-history tables side by side, export.
- Evaluation tab: shows eval/results.md and the charts.
- Audit tab (admin).
Walk through all demo scenarios in docs/outcomes.md and fix anything broken.

=====================================================================
PHASE H: Packaging and documentation
=====================================================================
1. docker-compose.yml: services api and ui. Ollama runs on the host (better GPU access on Windows);
   containers use OLLAMA_BASE_URL=http://host.docker.internal:11434. Volumes for data/, chroma/, the HF cache.
   data/raw is mounted, never baked into an image. Dockerfile(s) as needed.
2. README.md final: problem, architecture diagram, local-only design (no API keys, offline), data sources
   (AUTOSAR PDFs downloaded by script, not redistributed), setup on Windows (venv) and Docker, the run order
   (download_data → seed → ingest_all → extract_all → run_checks_all → uvicorn → streamlit), demo walkthrough,
   results table and charts from eval/results.md, limitations (honest), out of scope and scale path from docs/scope.md.
3. docs/DEMO.md: a 4-minute demo script mapped to the demo scenarios, with the exact questions to ask.
4. docs/FINAL_REPORT.md: what was built vs the case study requirements (requirement-by-requirement table with
   evidence: file, test, metric), evaluation summary, limitations, next steps.
5. Run the full test suite including -m llm, then a fresh-clone smoke test. Final commit.
````

---

## When it stops at F3

Open `eval/real_gold_candidates.csv`. Check each row against the PDF page it names, fix wrong rows, delete ones you can't confirm, set `verified=yes`, save it as `eval/real_gold.csv`, and type `continue`. It takes about 20–30 minutes, and it's what makes your real-data numbers credible.
