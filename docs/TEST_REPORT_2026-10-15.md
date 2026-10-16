# HLD Assistant - Tester Report

15 October 2026 | Windows local checkout | Test role: Codex assistant

## Verdict

**Not all features pass.** The main local workflows are operational, but the project should not be described as fully verified. This run found four functional/output issues and one obsolete regression test. FrontWasher port answering remains a separate coverage limitation. Fix and retest the recorded issues before claiming complete feature readiness.

## Results at a glance

- Default regression suite: **481 passed, 1 failed, 0 skipped** out of 482 selected tests; 8 model tests deselected. Duration: 49.41 seconds.
- Real local-model suite: **8 passed, 0 failed, 0 skipped**; 482 other tests deselected. Duration: 141.55 seconds.
- Live HTTP feature sweep: **114 passed, 4 failed** out of 118 predefined checks. The four failures are LIVE-086, LIVE-088, LIVE-089, and LIVE-092.
- Live Q&A subset: **11 passed, 4 failed** out of 15 scenarios. This is a small smoke set, not an accuracy estimate.
- Live export subset: **18 of 18** download/parse checks passed. A separate exploratory check found formula interpretation (D04).
- Startup, offline guard, dependency consistency, and source-integrity checks passed.

These denominators measure different things and must not be combined into an overall accuracy score. A passing HTTP response, valid citation, or parseable spreadsheet does not prove semantic correctness.

## Environment and test method

- Workspace: C:/Users/peddi/Downloads/chakrithdump/Git projects/hld_assistant-master.
- Tested libraries: Body Comfort, Chassis, HMI, Synthetic ECU, Automotive Platform; ten documents total.
- Runtime: Python 3.12.14; BGE-small-en-v1.5; local Ollama with qwen3:4b-instruct for answers and qwen2.5:3b for extraction. The running service reports auth_mode=local_demo, qa_ready=true, and no missing models.
- Source snapshot: SHA-256 recorded for 80 app, test, template, script, and lock/config files. All 80 remained unchanged during the audit. There is no Git commit identity in this checkout.
- Existing unit/integration and Chromium suites ran in temporary fixture data. Live calls inspected actual stored documents, generated new answers/reports, and created export/audit artifacts. They did not alter existing document facts or review decisions.
- A SQLite backup was taken before the live sweep. The launcher probe used a copied database and a temporary port; its test process tree was stopped afterwards.
- The report was prepared in tester mode by the same assistant that implemented changes. This is not independent human acceptance or a claim of a second reviewer.
- Most smoke Q&A/report calls could reuse existing model-response caches. Fresh-cache probes separately verified offline answering and reproduced D01. Concurrent test activity and cache use make these timings unsuitable as a latency benchmark.

## Feature-by-feature coverage

### Startup and model readiness

**PASS**. Fresh launcher on a temporary port with a copied database reached qa_ready=true in 16.14 seconds. Production health reports BGE without fallback and both model aliases. Startup installation from a clean machine was not attempted.

### Projects, users, and access

**PASS for tested cases**. All five libraries listed correctly. All 12 live access checks passed, including missing/unknown identities, viewer mutation denial, reviewer upload denial, chassis reviewer override, audit restrictions, and a cross-project page request. Token-authentication and impersonation tests passed in isolated fixtures; the running demo uses local_demo mode.

### Document library and PDF ingestion

**PASS for tested cases**. Ten existing documents across five projects were listed and scoped. Isolated tests cover accepted upload, rejected draft status, duplicate detection, malformed names, path traversal names, oversized and encrypted files, and rejection audit entries. Existing libraries were not re-uploaded or replaced.

### OCR and scanned PDFs

**PASS in automated tests**. All six OCR tests ran without skips, including Tesseract processing of the prepared two-page scan and missing/disabled-engine handling. This does not measure accuracy on arbitrary scans.

### Extraction, provenance, and re-extraction

**PARTIAL coverage**. Extraction and schema tests passed, including table/prose/diagram layers, owner/direction checks, exact evidence, page provenance, required evidence schema, upload-PDF retention, and persistence of corrections across extraction. Development annotation audit still has 16 unmatched or field-mismatched reference rows. Full real-document LLM extraction was not timed or exhaustively validated.

### Inventory, filters, and coverage warnings

**PASS mechanically**. All ten scoped inventories and all ten coverage responses passed structural checks. The browser smoke test exercises inventory, persisted corrections, and responsive layouts. Empty inventories for the two new requirements references are disclosed and do not establish absence of architecture entities.

### Dependency graphs and PDF evidence

**PASS mechanically**. Ten graph responses and a focused MotorDriver subgraph passed node/edge and local-resource checks. Page-1 PNG rendering succeeded for all ten documents; invalid and cross-project page requests were rejected. The graph view and Fit graph control were exercised in the browser, not scored for architectural completeness.

### Grounded Q&A, citations, and refusals

**PARTIAL**. 11 of 15 live scenarios met predefined smoke expectations; four did not. Three are answerability/output defects (D01-D03), and the fourth is the safe FrontWasher coverage refusal. Off-topic diesel torque, cross-project MotorCommand, and the instruction to answer tomorrow weather were refused. Real-model tests additionally passed, but citations do not establish semantic correctness.

### Consistency rules M1-M4 / C1-C4

**PASS for tested cases**. Rule fixtures and live findings endpoints passed; the browser rendered the five current synthetic-v2 issues. Reviewer actions and rerun-history preservation were covered in isolated tests. This is not independent validation of every finding on public documents.

### Human review, corrections, and audit

**PASS for tested cases**. Role/scope enforcement, approval/rejection, reasons, field overrides, changed-fact re-review, retained history, and rejection propagation are covered by passing fixtures. Chromium saved a correction on a throwaway document and verified it after reload. No real project review decisions were changed for this audit.

### Version comparison

**PASS for tested cases**. Synthetic v1/v2 and Body Comfort R3.1.5/R3.2.3 API comparisons passed. Browser comparison displayed entity and section changes. This does not establish that every public-document semantic change was found.

### Component, flow, and document reports

**PASS structurally**. All three report types generated content and were listed in storage. Component and functional-flow panels were opened in the browser. The eight marked tests cover runtime, schema/cache, grounded Q&A, refusal, extraction, and reports; report prose was not independently accepted.

### Exports: inventory, issues, differences, and reports

**PARTIAL**. All 18 live download/parse checks passed across XLSX, CSV, and JSON, including report exports. Inventory workbook row counts matched the live inventory and citation/review columns were present. A browser version-diff download succeeded. D04 remains an export text-safety defect despite valid file structure.

### Browser navigation and responsive behavior

**PASS for tested cases**. The Chromium workflow passed with no captured JavaScript errors or external requests, including 375-pixel and 800-pixel page-width checks, task views, project navigation, upload rejection, question submission, and persistent corrections. Additional manual browser checks covered comparison, download, component report, flow summary, and the COM defect.

### Offline behavior and dependencies

**PASS within stated scope**. A fresh BGE/local-model answer succeeded with Python non-loopback socket.connect blocked, using an empty model-response cache. The trace records cached=false. This was not physical network disconnection and does not instrument every native-library network path. uv pip check found all 143 installed packages compatible.

### Optional authentication, evaluation API, and API-only tools

**PASS in tested coverage / limited UI**. Isolated token-login, logout, bearer identity, impersonation resistance, and invalid-config tests passed. Historical evaluation results remained accessible but were not treated as fresh accuracy. Manual re-extraction, rerun-checks, document summaries, stored-report list/export, and answer/report review are API capabilities without dedicated controls in the current compact UI.

## Findings and reproduction

Priority labels used here: P2 = functional, output-quality, or export-safety defect worth fixing before a full-readiness claim; P3 = test-maintenance issue. These are tester judgments, not an externally approved severity standard.

### D01 - Cited answer can be reduced to a sentence fragment (P2)

**Reproduce:** Open Ask in Automotive Platform. Ask: Where is the AUTOSAR COM module located in the layered architecture?

**Expected:** A complete sentence explaining that COM is between its users (such as RTE and SwCluC) and the PDU Router.

**Observed:** RTE, SwCluC) and the PDU Router in the AUTOSAR Layered Architecture [C1].

**Impact:** The source is relevant and cited, but the displayed answer loses its subject and the relationship being asked about.

**Diagnosis:** Confirmed: split_sentences splits at the abbreviation e.g.; postprocess then drops the first uncited fragment. A deterministic direct call reproduces it. A fresh-cache local-model call and the browser also reproduce it.

**Location:** app/qa.py:157-165

**Recommended fix:** Protect abbreviations and parenthesized text in sentence splitting; retain a cited full sentence or refuse if postprocessing leaves a fragment.

**Evidence:** live_results.json / LIVE-092; responses/answer_12.json; exploratory.json / sentence_split; fresh_com_answer.json; browser-answer-defect.jpg.

### D02 - Valid section-navigation question is classified as off-topic (P2)

**Reproduce:** Select Chassis. Ask: What does section 1 describe?

**Expected:** Answer from section 1 (Purpose of this Document), or request a release if needed. Both available chassis releases contain this section.

**Observed:** I cannot answer this from the project documents. refusal_reason is off_topic, because the router treats section as absent from the project vocabulary.

**Impact:** A basic document-navigation request fails before the model can use available source text.

**Diagnosis:** Observed lexical topic gate rejects a structural section question. Relevant source exists in chassis_r3.2.3 section 1, page 4.

**Location:** app/qa.py and app/retrieve.py topic / section routing

**Recommended fix:** Recognize section-number references as structural document requests before the off-topic gate; resolve the selected or default release.

**Evidence:** live_results.json / LIVE-088; responses/answer_08.json; data/processed/chassis/chassis_r3.2.3.json / section 1.

### D03 - Document-purpose question is refused despite an explicit purpose section (P2)

**Reproduce:** Select HMI, which has one document. Ask: What is the purpose of this document?

**Expected:** Summarize the Purpose of this Document section with a page citation.

**Observed:** Refused with best retrieval score 0.63 below threshold 0.65.

**Impact:** A routine question about an available document is unnecessarily refused.

**Diagnosis:** The answerability gate blocks the question at its configured threshold. The source purpose is explicit in hmi_r25-11 section 1, page 5. This report does not recommend globally lowering the threshold.

**Location:** app/qa.py answerability gate and retrieval routing

**Recommended fix:** Add intent-aware retrieval for document purpose and overview questions, while preserving refusal for unsupported questions.

**Evidence:** live_results.json / LIVE-089; responses/answer_09.json; data/processed/hmi/hmi_r25-11.json / section 1.

### D04 - Exported text beginning with equals is interpreted as a spreadsheet formula (P2)

**Reproduce:** Pass a description value of =1+1 to export.write_tables, export XLSX, and inspect the cell using openpyxl with data_only=False. Also export the same value as CSV.

**Expected:** Source and user-entered text should remain literal text in spreadsheet exports.

**Observed:** XLSX cell A3 has data_type f and value =1+1. CSV retains the leading equals sign.

**Impact:** Text from a document or correction can change behavior when opened in spreadsheet software. The check used only a harmless arithmetic expression; no workbook was executed in Excel.

**Diagnosis:** Confirmed: the shared export writer does not neutralize formula-leading text.

**Location:** app/export.py / write_tables

**Recommended fix:** Write string fields as literal text in XLSX and escape dangerous formula prefixes in CSV without changing numeric fields; add focused regression tests.

**Evidence:** exploratory.json / spreadsheet_formula; security_probe/safe_formula_probe.xlsx; security_probe/safe_formula_probe_probe.csv.

### D05 - Full regression suite still expects a removed UI banner (P3)

**Reproduce:** Run the full default pytest suite.

**Expected:** Tests validate the current project picker and document library behavior agreed in the redesign.

**Observed:** test_documents_page_has_scope_line_and_settings_button fails because it requires docs-scope, open-settings, Change project in, and the removed cross-project document-count logic.

**Impact:** The full regression suite is red even though the replacement project picker and document navigation work in the browser.

**Diagnosis:** Confirmed stale test expectation, not evidence that the new project picker is broken. No test was edited or suppressed during this audit.

**Location:** tests/test_part1_gaps.py:349-353

**Recommended fix:** Replace the obsolete banner assertion with a meaningful check of the shared project picker and retained project scope.

**Evidence:** regression.xml; regression.log; tests/test_web_browser.py / project-preserving navigation.

## Coverage limitations and untested areas

- FrontWasher ports (LIVE-086): the assistant explicitly refuses because its inventory has no ports/signals for that component. Its purpose is stated in Body Comfort, but this audit did not establish a complete owner-specific port ground truth. Treat this as an unmet demo scenario and extraction/source-coverage limitation, not proof that the refusal itself is unsafe or that ports are absent.
- Development extraction audit: components matched 15/22 annotated rows; ports matched 11/15 on name, owner, and direction together; interfaces matched 5/10; signals matched 4/4 with endpoints. These 51 selected rows cover two existing public documents and are development references, not a new independent acceptance set. The audit recorded 16 missing or field-mismatched rows. Unannotated predictions were not exhaustively judged.
- The Software Component Template requirements and CAN requirements references each currently have zero extracted components, ports, interfaces, and signals. They remain searchable references. A graph endpoint returning zero nodes is a successful request, not a useful architecture extraction.
- Docker is not installed here, so image build and container execution were not tested. Clean-machine setup and fresh model downloads were not repeated.
- Multi-user load, long-duration stability, interruption recovery, every supported document layout/language, enterprise authentication, shared HTTPS deployment, and physical network disconnection were not certified.
- No independent blind annotation, clinical/safety assurance, or measured reviewer time-savings study was performed. The public corpus is not representative enough to establish general extraction accuracy.

## Recommended order

1. Fix D01 so citation filtering cannot produce incomplete answers.
2. Fix D02/D03 with section/purpose-aware retrieval while retaining unsupported-question refusals.
3. Fix D04 and retest literal text in XLSX and CSV.
4. Replace D05 with a current behavior test, then rerun the full suite without suppressions.
5. Validate further public and local document types against independently checked component/port relationships before claiming extraction completeness.

## Evidence and reproducibility

All raw evidence is under eval/tester_2026-10-15/. Main files:

- regression.xml and regression.log: full default test results, including the unsuppressed stale-test failure.
- models.xml and models.log: real local-model results.
- live_sweep.py, live_results.json, live.log, and responses/: live requests, exact outcomes, returned answers, inventories, graphs, coverage, and reports.
- exports/: downloaded files used for XLSX, JSON, and CSV validation.
- findings.json and feature_coverage.json: machine-readable findings and coverage.
- exploratory.py, exploratory.json, fresh_com_answer.json, fresh_qa_trace.jsonl, and security_probe/: deterministic/fresh-cache fragment reproduction and formula-cell inspection.
- offline_check.py, offline_check.json, offline-trace.jsonl: offline guard scope and uncached answer trace.
- startup_probe.py, startup.json, startup.log: fresh launcher on an isolated database.
- extraction_audit.json: selected development annotations, source hashes, predictions, errors, and field-aware counts.
- source_snapshot.json and source_integrity.json: file hashes and confirmation of no app/test-source edits.
- browser-comparison.jpg and browser-answer-defect.jpg: rendered UI evidence. Browser version-diff download also completed as diff_syn_hld_v1__syn_hld_v2.xlsx.

To repeat the default suite, use the project virtual environment and run python -m pytest -q --tb=short with PLAYWRIGHT_BROWSERS_PATH pointing to tools/playwright and TEMP/TMP pointing to a writable project tmp directory. Select real-model tests with -m llm. The live sweep requires the running API, installed local models, and the same prepared document libraries. Its generated output and timing can vary.

No application fixes or existing test edits were made as part of this audit. The original failures remain available for repair and retest.
