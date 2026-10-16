# Final report

Date: 2026-10-07 (updated after the Task 1-6 improvement round). Scope: solo pilot, local models only (Ollama), 4 GB VRAM.
Numbers come from `eval/results.md` / `eval/results.json` unless stated. "Not verified" means no human-checked reference
exists.

**State of the evaluation.** The latest full run (2026-10-07 14:04) covered **qwen2.5:3b**, the only model. A
first two-model run was stopped during its final control-test phase and saved nothing, so the frozen v4 document was
scored twice with an identical parser. Its LLM calls were cached, so nothing could be tuned between the two scorings.
llama3.2:3b was evaluated in an earlier round (git history, commit e5e7a81) and removed from the project by the author's decision; all results here are qwen2.5:3b.

## 1. Requirement-by-requirement evidence

### Functional capabilities (docs/project.md section 4)

| Requirement | Status | Evidence (module / test / metric) |
|---|---|---|
| Document upload and controlled ingestion | Met | `app/ingest.py`, `app/controls.py`; `tests/test_ingest.py`, `tests/test_controls.py`; control test `unapproved_upload_rejected` PASS |
| Section, table and metadata extraction (incl. scanned pages) | Met; OCR weak on gridded tables | `app/ingest.py`, `app/ocr.py`, `app/chunk.py`; `tests/test_ocr.py`; Tesseract 5.4 on `data/synthetic/scanned_sample.pdf`: prose page confidence 92.5, table page 58.9 (table text garbled); OCR chunks carry `ocr` / `ocr_confidence`, citations say "from OCR, confidence X" |
| Architecture entity recognition | Met on synthetic incl. held-out v4; partly on real | `app/extract.py`, `app/headers.py`, `app/diagram.py`; held-out v4 micro F1 0.97 (qwen) / 0.84 (deterministic); real gold (51 verified rows) F1 component 0.64, port 0.94, interface 0.67 (qwen) |
| Interface and dependency mapping | Met on synthetic, weak on real | `app/graph.py`; BodyComfort 205 edges, Chassis and HMI 0 edges (documents describe few ports) |
| Natural-language Q&A with citations | Met | `app/qa.py`; 25 gold questions: correctness 75%, citation accuracy 90%, refusal accuracy 100% (qwen) |
| Cited summaries and component reports | Met | `app/summarize.py`; claim sentences cited 100% after filtering (component reports and document summaries, qwen) |
| Revision comparison | Met | `app/compare.py`: entity diff (synthetic v1 to v2 9/9, 0 FP) + section diff (`tests/test_section_diff.py`): synthetic v1 to v2 5 modified sections; real R3.1.5 to R3.2.3 0 body changes, front matter only (legal disclaimer, version metadata), consistent with the documents' own change history |
| Potential mismatch and completeness checks | Met on synthetic | `app/checks.py`; v1 8/8, 0 FP; held-out v4 4/6 deterministic (0 FP), 5/6 qwen (7 FP) |

### Expected outputs

| Output | Evidence |
|---|---|
| Component and interface inventory | `extract.py` to SQLite, Inventory tab, `app/export.py`; BodyComfort R3.2.3 (deterministic) 49 comp / 131 port / 0 intf / 29 signal |
| Dependency maps | `graph.py` JSON + pyvis, `/projects/{pid}/graph?component=&depth=` |
| Functional-flow summaries | `summarize.functional_flow_summary`; cited steps on synthetic |
| Searchable Q&A with citations | `/projects/{pid}/ask`; citations, confidence, model name, OCR note where relevant |
| Potential inconsistency report | `checks.py`, Issues tab; real-document findings labelled "potential, needs review" |
| Structured export | `export.py` csv / xlsx / json for inventory, findings, diff (incl. `section_changes` sheet), reports |

### Inputs

| Input | Evidence |
|---|---|
| Approved / synthetic HLD documents | 5 real AUTOSAR PDFs (downloaded, not redistributed) + synthetic v1, v2, v3, held-out v4, scanned sample |
| Interface specs / architecture descriptions / component catalogues | `doc_type` accepted and stored; only the HLD-style path exercised with real data |
| Revision history | change-history table parsed into `revisions`; shown with the section diff in the Compare tab |

### Mandatory controls

| Control | Evidence |
|---|---|
| Approved / synthetic only | `ingest_rejected` audit row; control test PASS |
| Project isolation | control test `cross_project_query_empty` PASS |
| Access control | control test `viewer_cannot_approve` PASS; `/audit` 403 for non-admin |
| Answers restricted to evidence | similarity threshold 0.65 refuses (refusal accuracy 100% on 5 unanswerable); uncited sentences dropped |
| Source citations | doc_id, version, page, section on answers, entities, findings, reports |
| Confidence / limitation indicators | rule-based High / Medium / Low; OCR confidence note on OCR-derived citations |
| Human review before reliance | `review_status` starts `unreviewed`; reviews and audit stored; exports carry status |

### Deliverables D1-D14 (docs/outcomes.md)

D1 app: built. Docker is **not verified**: Docker Desktop starts, but `docker compose build` was stopped twice (44 min,
then 50 min) while downloading Python wheels at 40-100 kB/s; `up` was never run. D2-D8, D10, D13, D14: built and
tested. D9 evaluation report: `eval/results.md` (qwen2.5:3b run). D11 documentation: this file, README, DEMO,
`docs/TECHNICAL_REPORT.md`, `docs/MODEL_CONFIG.md`, `docs/CODE_WALKTHROUGH.md`, `docs/AI_TOOL_DECLARATION.md`,
`docs/CITATIONS.md` (drafts with [AUTHOR] slots). D12 demo video: not recorded; script in `docs/DEMO.md`.

## 2. Evaluation versus targets

| Success metric | Target | Result | Verdict |
|---|---|---|---|
| Extraction, synthetic v1 | P >= 0.85, R >= 0.80 | 1.00 / 1.00 (deterministic and qwen) | Meets, fit by construction |
| Extraction, **held-out v4** (frozen before parser changes, `eval/heldout_v4.lock`) | none set | qwen micro P 1.00 R 0.95 F1 0.97; deterministic P 1.00 R 0.73 F1 0.84 (components R 0.00 without the LLM; ports R 0.88) | Good generalisation result, with the caveats below |
| Extraction, synthetic v3 (no longer held-out) | none | 1.00 (all modes), was 0.25 qwen / 0.00 deterministic before the parser changes | In-sample: shows the diagnosed failures are fixed, not generalisation |
| Extraction, real documents (51 verified gold rows, `eval/real_gold.csv`) | P >= 0.85, R >= 0.80 | qwen: component R 68% P 61% F1 0.64; port R 93% P 94% F1 0.94; interface R 50% P 100% F1 0.67; signal R 100% (n=4, P n/a) | Ports meet; components and interfaces miss. Precision is page-scoped (gold not exhaustive) |
| Answer groundedness (citation accuracy, doc + page) | >= 90% | 90% | meets |
| Planted defects found | >= 6 of 8 | v1 8/8 | Meets |
| False positives | <= 2 | v1 0; v4 0 deterministic, 7 qwen (all C4 "missing description" on LLM-found components) | Meets on v1; qwen exceeds on v4 |
| Revision comparison | >= 90% | 100% (9 of 9) | Meets |
| Review effort (time saved) | report measured reduction | not measured; protocol and timer ready (`docs/review_effort_protocol.md`, `scripts/review_timer.py`) | Missed until the author runs the protocol |
| Summary groundedness | >= 90% | 100% component reports, 100% document summaries (qwen; 82% cited before filtering) | Meets, high by construction |
| Confidence calibration | High clearly above Low | qwen High 67% (6/9), Medium 82% (9/11), Low 100% (5/5) | Misses: High is below Medium; Low is mostly refusals |
| Control enforcement | 100% | 4 / 4 | Meets |
| Local feasibility, latency | report | qwen Q&A generation call p50 2.9 s / p95 9.5 s; extraction per call p50 3.7 s; VRAM peak 3.4 of 4 GB | Reported. The Q&A end-to-end row in results.md has n=3 because the run was almost fully cached; use the generation-call row |

**Held-out v4 caveats.** v4 and the parser changes were both produced by AI agents in the same session. They were
separate agents, and the parser agent was forbidden from opening any v4 file. The v4 style brief named header words
(SW-C, Port Prototype, P/R, Data Element) that also appear in the generic synonym list the parser agent was given, so
v4 is easier for this parser than a truly independent document. Deterministic components R 0.00 on v4 shows a remaining
gap: v4's components appear in a layout the table/heuristic layers do not read, and only the LLM recovers them. The
qwen run then produces 7 C4 false positives, because LLM-found components carry no description.

Model: `qwen2.5:3b` (author decision); `DEMO_MODEL` and `OLLAMA_MODEL` both default to it; see README.

### Tests

- `pytest -q`: 233 passed, 8 deselected (llm).
- `pytest -q -m llm`: 8 passed.

### Other measured items

- Section diff counts (added / removed / modified body / front matter): BodyComfort 0/0/0/2, Chassis 0/0/0/2, synthetic
  v1 to v2 0/0/5/2. A first pass reported 7-8 "modified" sections per real pair; every one was extraction noise
  (re-ordered figure labels, glued release strings), now filtered. Limitation: a real edit that only reorders existing
  words is also hidden.
- Demo reset (`scripts/demo_reset.py`, scratch target, machine busy with other jobs): 537 s total; ingest of real PDFs
  133 s and the first LLM report 219 s dominate. API warm-up: `/health` reports warming / ready / degraded.
- A `.gitattributes` fix: with `core.autocrlf=true` git rewrote line endings inside reportlab PDFs on checkout
  (corrupted the v4 PDF); PDFs, PNGs and XLSX are now binary.

## 3. Limitations

- Real-data extraction misses the target for components (F1 0.64) and interfaces (0.67); ports meet (0.94). Gold has 51
  rows from two documents and precision is page-scoped.
- Held-out v4 is AI-written by the same session (separate agent) and partly shares vocabulary with the synonym list;
  v3 is in-sample now.
- LLM-found components lack descriptions, which triggers C4 false positives (7 on v4 with qwen).
- OCR reads prose well (confidence ~92) but garbles gridded tables (~59); no table detection on scanned pages.
- Chassis has 0 ports and 0 edges, HMI few: real port inventories are mostly absent from these documents.
- Calibration: High-confidence answers are less accurate than Medium (25 questions).
- Docker image build never completed on this connection; container path untested.
- Review-effort reduction not measured; no demo video.
- Header-based pilot auth is not secure; spreadsheet formula injection is not neutralised in exports.
- Groundedness is high by construction; lexical support is a weak proxy for entailment.

## 4. Next steps

1. Run the review-effort protocol (`docs/review_effort_protocol.md`) and re-run the evaluation to report the reduction.
2. `docker compose build` / `up` on a faster connection; fix what breaks.
3. Give LLM-found components a cited description (or exempt them from C4); measure component extraction on more real
   documents.
4. Fill the [AUTHOR] slots in the Task 6 drafts; record the demo video; install mermaid-cli and rebuild
   `docs/TECHNICAL_REPORT.pdf` (`scripts/build_report_pdf.py`).
5. A truly independent held-out document (written by a person, not by this session).
