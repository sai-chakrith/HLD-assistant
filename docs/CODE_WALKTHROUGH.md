# Code walkthrough

For the author to study before the viewing. Each section: what the module does, its main functions, the key design decision and
why, and two questions an evaluator is likely to ask with answers grounded in the code. File and line references are to the
repository state of 2026-10-07 (line numbers drift; the function names do not). Read the code behind every answer before
repeating it: **[AUTHOR]** mark the answers you could not reproduce yourself.

Order follows the data flow: `config`, `controls`, `ingest` (`ocr`, `headers`), `chunk`, `retrieve`, `extract` (`diagram`), `graph`,
`checks`, `compare`, `llm`, `qa`, `summarize`, `export`, `api`, then the UI.

## Overview of the data flow

PDF -> `ingest.parse_pdf` -> processed JSON (`data/processed/<project>/<doc>.json`) -> `chunk.chunk_document` -> `retrieve.index_chunks`
(Chroma). The same JSON plus the PDF -> `extract.extract_document` -> SQLite entity tables -> `graph`, `checks`, `compare`, `export`,
`summarize`. Questions: `qa.ask` -> `retrieve.search` -> `llm` -> `qa.postprocess`. `api.py` exposes all of it, `controls.py` guards it.

---

## app/config.py

**What it does.** One frozen `Settings` dataclass, built once at import from environment variables and `.env`, with a default for every
value. Paths are resolved against the project root. The module-level `settings` is imported everywhere.

**Main functions.**
- `load_settings(env_file=None)`: reads `.env` (without overriding real environment variables) and builds `Settings`.
- `_path(value)`: makes a path absolute relative to the project root.

**Key decision.** No hard-coded thresholds in the modules that use them (similarity, chunk sizes, diagram tolerances, OCR switch, model names):
they are settings, so the evaluation and the tests can change them and the diagram tolerances can be tuned without editing code. Cloud
provider names do not exist here, only `ollama` and `none`.

**Likely questions.**
1. *Why is the similarity threshold 0.65 and where did it come from?* It is `SIMILARITY_THRESHOLD`. It was chosen on the project's own questions
   (answerable ones score above it, off-topic ones below, e.g. the diesel-torque question scored 0.54); it is only valid for the bge embedder, which is why
   Q&A refuses when the hashing fallback is active. **[AUTHOR]** point to `scripts/tune_qa.py` and `docs/data_notes.md` for how it was tuned.
2. *How do you switch the model?* `OLLAMA_MODEL` for scripts and evaluation, `DEMO_MODEL` for interactive API requests, or the `model` field per request; names
   outside `ALLOWED_MODELS` are rejected in `llm.validate_model`.

## app/controls.py

**What it does.** SQLite schema and connection, project/user/membership management, per-project role checks, the audit log and review decisions.

**Main functions.**
- `get_conn(path)` / `migrate`: open the database with the schema ensured; idempotent migration that copies `users.role` into `project_members.role`.
- `create_project`, `add_user`, `add_member`: seeding helpers.
- `require_role(user, project_id, min_role)`: raises `PermissionDenied` unless the user is a member with a role at least `min_role` (viewer < reviewer < admin).
- `log_action(user, action, target, details)`: appends to `audit_log`.
- `review(item_type, item_id, decision, comment, user, project_id)`: reviewer or admin only; writes the `reviews` row, updates the item's `review_status`, logs the action; a denied attempt is logged as `review_denied`.
- `list_reviews`.
- `ROLES`, `DOC_TYPES`, `SOURCE_STATUSES` (`approved`, `synthetic`), `DECISIONS`, `REVIEW_TABLES`: the constants that define the controls.

**Key decision.** Authorisation uses only `project_members.role`; the global `users.role` is just the default for new memberships and the flag that gates the audit log.
So a global admin who is not a member of a project is still refused there, which keeps project isolation strict. The schema also has `CHECK` constraints on role,
doc type and source status.

**Likely questions.**
1. *How is project isolation enforced?* Three places: the API calls `require_role` on every project endpoint; SQLite rows carry `project_id`; Chroma has one collection per project
   (`retrieve.collection_name`) and every query also filters by `project_id`. The evaluation's `cross_project_query_empty` control checks it.
2. *Is the authentication secure?* No. The API trusts an `X-User` header naming a seeded user (`api.get_user`); this is stated as a pilot limitation and the API is meant for localhost.
   The scale path is OIDC in front of the API.

## app/ingest.py

**What it does.** Controlled PDF ingestion: validates the upload, parses the PDF into sections, tables, change history and metadata, stores a processed JSON and a `documents` row,
chunks and indexes the document, and audit-logs the outcome.

**Main functions.**
- `ingest_document(path, project_id, doc_type, version, source_status, user, ...)`: requires admin role in the project; rejects (and audit-logs `ingest_rejected`) a status other than approved/synthetic,
  an invalid doc type, a non-PDF, a file whose first bytes are not `%PDF`, a duplicate sha256 in the project, or an existing doc id; parses; writes JSON; inserts the row; indexes.
- `parse_pdf(path, doc_id)`: streams pages; for each page takes text lines (`_page_lines`: outside the header/footer zones, with size, bold flag and bounding box), falls back to OCR when there is no text layer,
  skips table-of-contents pages, finds tables with pdfplumber (`_page_tables`), removes lines that lie inside table boxes, detects numbered headings (`_detect_heading`) and feeds a `_Builder` that
  accumulates sections.
- `_classify_table`, `_header_valid`, `_roles_ok`, `_aligned_rows`: decide whether a pdfplumber table is a real table (header recognised fuzzily through `headers.py`, or column roles clear from the cell values),
  keep empty cells so columns do not shift, and merge continuation tables across pages.
- `_has_toc`: documents without a table-of-contents page start numbered headings on page 1 instead of page 5.
- `_revisions`, `_metadata`: change-history rows and the page-1 key/value metadata.
- `load_processed`, `processed_path`, `iter_tables`, `file_sha256`: helpers.

**Key decision.** Parse once into a plain JSON (sections with page ranges, tables, OCR flags) and let everything else read that JSON, so extraction, chunking, diffing and summaries are re-runnable
without re-reading the PDF and each item can cite a page. Rejections happen before any parsing and are logged, which is the evidence for the "approved or synthetic only" control.

**Likely questions.**
1. *How do you know a heading is a heading?* A line starting with a section number whose font is bold or larger than body text, with page and numbering rules (`_detect_heading`, `HEAD_RE`); in OCR text, where there is
   no font information, only a short `N Title` line counts. Different layouts (no TOC page, two columns) were handled in Task 3b; **[AUTHOR]** know which synthetic document tests which case (`tests/test_generalisation.py`).
2. *What happens with a scanned page?* `ocr.is_scanned(page)` (almost no text and at least one image) triggers `ocr.ocr_page`; the lines go through the same heading and section logic, the section is flagged `ocr` with the
   mean confidence, and the `ocr_pages` list records status per page (including `tesseract_missing`).

## app/ocr.py

**What it does.** OCR of scanned pages with Tesseract.

**Main functions.**
- `is_scanned(page)`: fewer than 20 characters of text and at least one image.
- `tesseract_available()`: configures pytesseract (`TESSERACT_CMD`, PATH, default Windows path) and checks the binary runs.
- `ocr_page(page)`: renders at 300 dpi, runs `image_to_data`, groups words into lines by (block, paragraph, line), computes per-line mean confidence, scales boxes back to PDF points, promotes a short `N Title` line to a heading,
  and returns `{status, lines, confidence, chars}`. It never raises: disabled, missing binary and errors all come back as a status.

**Key decision.** OCR output is shaped exactly like native text lines (`size`, `bold`, `bbox`, `block`, plus `ocr_conf`) so the rest of the parser needs no special case, and the provenance (flag and confidence) is carried into chunk
metadata and into the answer's limitation text instead of being hidden.

**Likely questions.**
1. *How accurate is the OCR?* There is no accuracy measurement in the evaluation; the unit tests check the plumbing and a synthetic scanned sample is used. The system reports the Tesseract confidence rather than claiming accuracy.
   **[AUTHOR]** if you measure it, say so.
2. *What if Tesseract is not installed?* The page is kept with empty text, `ocr_pages` records `tesseract_missing`, a warning is logged once, and ingestion continues.

## app/headers.py

**What it does.** Shared table-header vocabulary: maps a header cell to a canonical field (`component`, `port`, `interface`, `direction`, `kind`, ...) and infers column roles from cell values when headers are unknown.

**Main functions.**
- `hn(s)`: normalisation (lower case, keep letters, digits and `/`).
- `match_header(cell)`: exact alias, then special cases (`interface kind`, `port direction`), then the head noun (the last significant word of a compound header), then rapidfuzz on single words above a cut-off.
- `is_header_row(row)`: true only when every cell is a recognisable header word, so data rows are never taken for headers.
- `infer_roles(rows)`: from value patterns (P/R, S/R or C/S, `Pp_`/`Rp_` ports, `I...` interfaces, data types, a leftover CamelCase column as component); needs at least 80% of at least two non-empty cells to match.

**Key decision.** Replace exact header lists with fuzzy and pattern-based inference, conservatively (high match fractions), after the first held-out document showed the deterministic parser found nothing when header words changed.
The conservative thresholds are the guard against turning prose into fake tables.

**Likely questions.**
1. *Isn't this over-fitted to your synthetic documents?* That is the main risk and the reason for the frozen v4 document (`eval/heldout_v4.lock`): v4 was generated before these changes. State the caveat that the same AI session produced
   both the generator and the change (`docs/TECHNICAL_REPORT.md` section 5.1), and that real-document entity sets were byte-identical before and after (`eval/v3_after_parser_changes.md`).
2. *What stops a prose table from being parsed as a table?* `is_header_row` requires every cell to be a header word and `infer_roles` requires a dominant pattern per column with at least two rows; failing both, the table stays text.

## app/chunk.py

**What it does.** Splits each section into retrieval chunks and each table into markdown chunks.

**Main functions.**
- `est_tokens(text)`: 1.3 tokens per word estimate.
- `split_text(text, target, max_tokens, overlap)`: packs sentence-like units into chunks of about the target size, never above the maximum, carrying an overlap tail forward; very long units are split by words.
- `table_to_markdown`, `_table_chunks`: tables become their own chunks with the header repeated when split.
- `chunk_document(doc)`: returns `[{id, text, metadata}]`; a chunk never spans two sections; each text is prefixed with `"<section id> <title>"`; metadata carries project, document, type, version, page range, section, chunk type and the OCR flag and confidence.

**Key decision.** Section-aware chunks with the section title in the text, so a retrieved chunk is self-describing and a citation can always name the page and section; page range comes from the section, so
citations are as precise as the section's page span.

**Likely questions.**
1. *Why 500 tokens and 50 overlap?* Defaults (`CHUNK_TARGET_TOKENS`, `CHUNK_OVERLAP_TOKENS`); five chunks of at most 600 tokens plus the prompt fit the 4096-token context. **[AUTHOR]** say whether you tested other sizes (the project did not run a chunk-size study).
2. *Does the token estimate matter?* It is rough (1.3 tokens per word), which is why the maximum is below the context limit with margin; the LLM extraction path chunks by characters instead.

## app/retrieve.py

**What it does.** The vector store layer: embedder selection, one Chroma collection per project, indexing and search.

**Main functions.**
- `get_embedder()`: lazy singleton `BgeEmbedder` (sentence-transformers on CPU, normalised vectors) or `HashingEmbedder` if loading fails or `EMBEDDER=hash`.
- `embedder_status()`: reports kind and whether the fallback is active; `qa` refuses when it is.
- `index_chunks(project_id, chunks)`: refuses chunks whose metadata project differs from the target, upserts in batches of 64 into a cosine collection.
- `search(project_id, query, k, filters)`: prefixes the query with the bge instruction, queries only this project's collection, returns text, metadata and score = 1 - distance.
- `collection_name(project_id, embedder)`: safe and collision-free name including a hash and the embedder kind, so bge and hash indices never mix.
- `delete_document`, `reset_embedder`.

**Key decision.** Hard project separation at the collection level rather than a filter alone, and an explicit fallback that is visible instead of silently producing meaningless scores.

**Likely questions.**
1. *Why bge-small and not a larger embedder?* It runs on CPU in the 4 GB GPU budget left by Ollama, is small, and performed adequately on the 25-question set; no embedder comparison was run. **[AUTHOR]** be ready to say that.
2. *Why a query prefix?* bge models are trained with an instruction prefix for queries (`QUERY_PREFIX`); documents are embedded without it.

## app/extract.py

**What it does.** Entity extraction (components, ports, interfaces, signals) with method, confidence and exact-page sources; the merge of five layers; storage in SQLite, review-preserving.

**Main functions.**
- Models `Source`, `Component`, `Port`, `Interface`, `Signal` (pydantic); each entity has `extraction_method`, `confidence`, `review_status` and a list of sources.
- `classify_table`, `entities_from_table`, `map_headers`: table layer (uses `headers.py`).
- `heuristic_entities`, `prose_ports`, `is_container_heading`, `component_name_from_title`: heading and sentence rules; the C4-noise rule keeps generic headings (Overview, Description, ...) from becoming components.
- `extract_from_prose`, `chunk_section_text`, `prose_text`, `_quote_ok`, `_name_ok`: LLM layer: skips front matter and short sections, removes diagram-label noise, sends chunks of at most 3000 characters with a JSON schema, keeps an item only if its evidence quote
  and name occur in the section text.
- `infer_by_convention`: name-prefix inference (never overrides).
- `merge_entities`: precedence table > diagram > heuristic > llm > convention; keeps all sources; lower-ranked sources only fill empty fields; agreeing methods raise confidence one step.
- `extract_entities(doc, ...)`: pure function running all layers; `extract_document(...)`: stores results, updating matching rows in place so review status survives and deleting only stale unreviewed rows.
- `get_inventory`, `method_counts`, `normalize_name`.

**Key decision.** Deterministic layers first, LLM last and sandboxed by the evidence-quote check, with a merge that keeps every source and ranks by trustworthiness. The LLM is never the sole judge of what exists in a document, and the
review history survives re-extraction.

**Likely questions.**
1. *How do you stop the LLM from inventing entities?* JSON-schema output plus a verbatim evidence quote of at most 25 words that must occur in the section, plus the entity name must appear in the text and pass `_name_ok` (no generic words, no repeated tokens, no figure artefacts). Dropped items are counted in the
   extraction stats. It does not stop the model from picking a real phrase that is not an entity, which is why real-document precision is stated as unknown.
2. *Why do the two models give very different component counts on the real documents?* The LLM layer is noisy and unstable between models (the numbers are in section 2 of the report); the deterministic layers give the same result for both, and the merge precedence prevents the LLM from overriding them.

## app/diagram.py

**What it does.** Extracts components, ports and signals from vector drawings in the real PDFs.

**Main functions.**
- `analyze_pdf(path)` / `analyze_page(page, page_no)`: per page, flattens `get_drawings()` into boxes, arrow-head triangles, port markers (small squares) and line segments (`_collect_primitives`), chains segments into arrows (`_chain_segments`, `_build_arrows`), reads text lines, then assigns: a title line to the
  smallest box containing it, a port label to the box with the nearest border (inside within a margin, or outside near a marker or arrow end), a signal label (`Name / kind`) to the nearest arrow, arrow ends to the boxes they touch. Port direction: provided if an arrow starts at the port's border position,
  required if one ends there.
- `page_qualifies(pd)`: only pages with at least two signal labels or three port markers count as interface diagrams, so conceptual block diagrams (Chassis) are skipped.
- `extract_diagram_entities(doc, pdf_path)`: builds `Component`, `Port`, `Signal` entities with page-exact `Source(method="diagram")` and confidence High when label-to-border distances are within `DIAG_HIGH_DIST`.
- `find_pdf(doc)`: locates the source PDF by sha256 (raw and synthetic dirs) or `<doc_id>.pdf`.
- `render_debug(...)`: draws boxes, titles and ports on a page image (`docs/diagram_checks/`) for visual checking.

**Key decision.** Geometry instead of a vision model: the AUTOSAR figures are vectors, so exact coordinates are available and a 4 GB GPU is not needed. Every tolerance is a setting. The cost is hand-tuned thresholds and no generality to raster diagrams.

**Likely questions.**
1. *How do you know the diagram extraction is right?* Visual checks with `render_debug` images and spot comparison against a candidate list; there is no verified real-data precision or recall (`eval/real_gold.csv` is not verified). The provisional recall figures in `eval/results.md` are against unverified candidates.
2. *Would it work on a scanned or raster diagram?* No. Raster figures have no drawing primitives; that would need a vision model, which is in the scale path.

## app/graph.py

**What it does.** Builds a networkx multi-digraph from the stored inventory, and component-level flow queries.

**Main functions.**
- `build_graph(project_id, doc_id, version)`: nodes `component:`, `port:`, `interface:`, `signal:`; edges `has_port`, `uses_interface`, `sends`, `receives`, `provides_to` (a provided port to a required port with the same normalised interface on another component).
- `component_flow_graph`, `dependencies_of`, `dependents_of`, `paths(a, b)`: provider/sender to consumer/receiver, upstream, downstream, simple paths.
- `export_json`, `export_pyvis`, `graph_dir`: JSON and interactive HTML for the UI.

**Key decision.** The graph is derived data computed from the SQLite inventory, not a second source of truth; component-level direction is provider/sender to consumer/receiver so "depends on" has one meaning everywhere (checks, reports, UI).

**Likely questions.**
1. *Why networkx and not Neo4j?* A single-document graph of tens to hundreds of nodes fits in memory; Neo4j is listed in the scale path.
2. *How are dependencies between components decided?* From matching interface names between a provided port of one component and a required port of another, and from signal sender and receivers; when ports are unknown (Chassis, HMI) there are no edges, which is a limitation of the data, not a bug.

## app/checks.py

**What it does.** Eight deterministic rules over the extracted inventory; no LLM.

**Main functions.**
- `rule_m1` .. `rule_m4`, `rule_c1` .. `rule_c4`: pure functions returning `Finding`s (rule id, severity, explanation, entities, evidence pages).
- `run_rules(inv, flows)`: runs them all; `flow_sections(doc)`: sections whose title matches `flow`.
- `run_checks(project_id, doc_id, version)`: stores findings (idempotent per document), labels findings on approved real documents "potential, needs review"; `get_findings`.

**Key decision.** Findings come from rules over structured data so each one can be explained by the entities and pages involved, and never from model opinion. Rules that cannot apply are skipped (C1 is skipped when a document has no flow section; unknown interface kinds never count in M2).

**Likely questions.**
1. *How does M4 decide that two names are near-duplicates?* rapidfuzz ratio of the normalised names at or above 88 (`FUZZY_THRESHOLD`) while the normalised names are not identical; applied to components and to interfaces.
2. *Why are the real-document findings mostly C4 (missing description)?* The real extraction has few ports and descriptions, so the rules that need richer data (M1, M2, M3) rarely fire and C4 dominates; they are labelled potential for that reason.

## app/compare.py

**What it does.** Two comparisons between releases: entity diff and section-level text diff.

**Main functions.**
- `compare(project_id, doc_a, ver_a, doc_b, ver_b)`: diffs stored inventories per type: added, removed, changed (only meaningful fields, after normalising and sorting), possible renames (fuzzy score at least 88, listed in addition),
  `llm_only_unmatched` (entities supported only by the LLM layer, listed apart); attaches the section diff and the change-history rows.
- `_diff_type`, `_index`, `_norm_value`: the keyed comparison.
- `section_diff(doc_a, doc_b)`: aligns sections by number and title (`_align`), then title alone, then fuzzy title (85); `_compare_sections` compares cleaned text sentence by sentence and table rows; `_clean_lines` strips headers, footers, release strings;
  `_drop_moved` removes text that merely moved between sections; front matter and change-history go to a separate bucket.

**Key decision.** Compare normalised meaningful content and keep uncertain cases visible rather than counting them as changes (renames listed under both added/removed and possible renames, LLM-only entities in a separate list, moved words dropped). The section diff exists because the entity diffs of the
real releases are empty (the releases differ in a legal disclaimer and page numbering).

**Likely questions.**
1. *What does the section diff report for the real R3.1.5 vs R3.2.3 pair, and is it correct?* **[AUTHOR]** run it and read the output; the change-history tables differ by one row (BodyComfort) and the disclaimer text changed (`docs/data_notes.md`). There is no accuracy figure for the section diff, only unit tests.
2. *Why not diff the PDFs directly?* Raw text diffs are drowned in page headers, footers and re-flowed lines; aligning by section and cleaning noise first is what makes the result readable.

## app/llm.py

**What it does.** The only LLM access layer: local Ollama or none, with schema-validated structured output, a SQLite response cache and a JSONL trace.

**Main functions.**
- `get_provider(name)`: `ollama` or `none`; anything else raises (cloud providers disabled by policy). `NullProvider` raises `LLMNotConfigured` on every call.
- `OllamaProvider.generate(prompt, system, schema, model, purpose, chunk_ids, use_cache)`: key = sha256 of model, system, prompt and schema; cache hit returns immediately; otherwise posts to `/api/generate` with `temperature` 0.1 and `num_ctx` 4096 (and `format` = JSON schema when given); logs a trace line either way.
- `_generate_validated`: for schema calls, parses and validates; on failure appends the error to the prompt and retries up to twice, then raises `ValueError`.
- `validate_model`, `ollama_status`, `cache_get`, `cache_put`, `cache_clear`, `write_trace`.

**Key decision.** One narrow interface that every caller must catch `LLMNotConfigured` on, so the whole application degrades (evidence-only answers, structured facts without a purpose paragraph) instead of failing when no model is reachable, plus a cache that makes evaluation and demos repeatable.

**Likely questions.**
1. *Is the output reproducible?* Only through the cache. No seed is set, and temperature is 0.1, so uncached runs can differ; the evaluation notes this and the tables are computed from cached calls.
2. *Why `num_ctx` 4096 with a model that supports far more?* VRAM: a 4 GB GPU holds a 3B model at 4-bit with a 4K context; the code chunks prompts (5 chunks, 3000-character extraction chunks, 1800-character summary pieces) to fit.

## app/qa.py

**What it does.** Grounded question answering with a refusal threshold, citation filter and rule-based confidence; stores each answer and logs it.

**Main functions.**
- `ask(project_id, question, user, k=5, ...)`: requires viewer role; calls `_answer`; stores the answer (status `unreviewed`) and audit-logs it.
- `_answer`: refuses if the hashing embedder is in use; retrieves; refuses if the best score is below the threshold; builds the prompt (`build_prompt`, chunks labelled `[C1]`..); calls the model; on `LLMNotConfigured` returns evidence-only mode; post-processes; refuses if no valid citation remains.
- `postprocess(text, lmap)`: splits into sentences, removes invented labels, drops sentences without a valid label, keeps "Not stated in the provided documents" notes, returns the clean text, the labels used and the cited share.
- `compute_confidence(mode, top_score, n_valid, cited_share)`: Low if not generated, no valid citation or cited share below 0.5; High if top score at least 0.75, at least two distinct cited chunks and cited share at least 0.8; else Medium.
- `parse_citations`, `split_sentences`, `label_map`, `_citation` (adds OCR note), `Answer` and `Citation` models.

**Key decision.** Filter after generation instead of trusting the prompt: sentences without a valid citation are deleted, and if none remain the system refuses. Confidence never comes from the model. Three distinct degraded modes (refused, evidence-only, generated) are explicit in the `mode` field.

**Likely questions.**
1. *Does a valid citation mean the sentence is true?* No. It means the sentence points at a retrieved chunk that exists. Truth is only approximated by the key-fact check on the gold set, and the report states that groundedness is high partly by construction and that lexical overlap is a weak entailment proxy.
2. *Is the confidence calibrated?* Not clearly: in the evaluation Low answers are mostly refusals, and High is not much more accurate than Medium for one model (section 5.4 of the report).

## app/summarize.py

**What it does.** Cited component reports, map-reduce document summaries and deterministic functional-flow summaries.

**Main functions.**
- `component_report(project_id, doc_id, version, component, ...)`: structured part from the inventory and graph (ports, interfaces, upstream, downstream, signals, source pages, every item with a citation) plus an LLM-written purpose paragraph built by `_component_purpose` from retrieved chunks `[C]` and facts `[F]`.
- `document_summary(...)`: map step per section piece (labelled `[S]`), reduce step over the bullets, at most six sentences, front matter and TOC skipped.
- `functional_flow_summary(...)`: deterministic; parses numbered steps from flow sections (or derives flows from graph edges when none exist), cites each step, flags entity names not in the inventory.
- `cited_filter(text, valid, texts, names)`: the shared filter: keeps sentences with a valid label, strips invented labels, requires at least 40% lexical overlap with the cited text and may re-point a sentence to the chunk that best supports it.
- `report_confidence`, `parse_steps`, `_pieces`, `_store`.

**Key decision.** Facts are assembled by code and carry their own citations; only the prose paragraph comes from the model, and it passes through the same delete-what-is-not-cited filter as Q&A, with an added lexical-support check because labels are wrong more often than claims.

**Likely questions.**
1. *Why is the flow summary not LLM-written?* The flow sections already contain ordered steps; parsing them is exact and cheap, and every step keeps its true page. The LLM would add risk without adding information.
2. *What does "100% cited" for reports mean?* After filtering every kept sentence has a valid label by construction; the harness also records the share the model cited before filtering, and the document summaries show the gap for one model (see the 'before filtering' rows in `eval/results.md`).

## app/export.py

**What it does.** CSV, Excel and JSON export of inventory, findings, diffs and stored reports.

**Main functions.**
- `write_tables(tables, ...)`: generic exporter from `{name: rows}`.
- `inventory_tables`, `findings_rows`, `diff_tables`, `report_tables`: build the rows; every row carries `review_status` and `doc_id`, `version`, `page`, `section`.
- `export_inventory`, `export_findings`, `export_diff`, `export_report`, `export_stored_report`: convenience entry points.

**Key decision.** Exports are self-describing: Excel puts a "Contains unreviewed AI output" note in row 1 of every sheet whenever any row is unreviewed, JSON carries the same note, and CSV relies on the `review_status` column.

**Likely questions.**
1. *Why a banner row?* So an exported file passed to someone else cannot be mistaken for reviewed data; the review status is also in each row.
2. *Is spreadsheet formula injection handled?* No. `_safe` only sanitises file names, and `_cell` only flattens lists and dicts to text; a cell value starting with `=` from a hostile document would be written as is. State it as a known limitation (verified by reading `app/export.py`), and note exports are made from the user's own project data. **[AUTHOR]** decide whether to fix it before submission.

## app/api.py

**What it does.** FastAPI service: every project endpoint checks membership and a minimum role; paths come from a `get_paths` dependency so tests can swap them.

**Main functions (endpoints).**
- `/health` (models, Ollama reachability, embedder status, warm-up state), `/me`, `/projects`.
- `/projects/{pid}/upload` (admin): ingest, then `_pipeline` = extraction (diagram, convention, heuristics; LLM only when asked) and the deterministic checks; the PDF is kept so evidence pages can be rendered.
- `/projects/{pid}/extract`, `/documents`, `/inventory`, `/graph`, `/findings`, `/run-checks`, `/ask`, `/compare`, `/review`, `/export`, report endpoints (`component`, `document`, `flow`, `reports`), `/documents/{doc}/pages/{n}.png`, `/eval/results`, `/audit` (global admin only).
- `_warmup`: loads the embedder and sends a one-token request to `DEMO_MODEL`; never raises; `WARMUP` shows `warming`, `ready` or `degraded` with a reason.
- `_demo(model)`: the request's model, else `DEMO_MODEL` if allowed.

**Key decision.** All business rules live in the modules under `app/`, the API only wires them and enforces roles, and the UI talks to the API only. Start-up warm-up is asynchronous so the API is available immediately and reports honestly when the model is not ready.

**Likely questions.**
1. *Why is there both `DEMO_MODEL` and `OLLAMA_MODEL`?* Both default to `qwen2.5:3b`, the only model. They are kept separate so a later model trial can change one without the other.
2. *What happens if Ollama is down?* `/health` shows it, warm-up reports `degraded`, `/ask` returns evidence-only mode, reports show the structured facts with "LLM not configured". The API does not crash.

## app/web.py, app/templates/, app/static/

**What it does.** The browser UI, served by the same FastAPI app (`app.include_router(web.router)` and a `/static` mount at the end of `api.py`). `web.py` has four page routes: `/` Documents, `/ask` Ask, `/architecture` Architecture, `/review` Review. Each renders a Jinja2 template that extends `base.html` (top bar with the screens, project selector, user selector, API / Ollama status dot). The page is a shell; its data is loaded by one ES module per screen (`static/js/documents.js`, `ask.js`, `architecture.js`, `review.js`) through `static/js/api.js`, which adds the `X-User` header, turns API errors into plain-language messages, builds DOM nodes as text (never `innerHTML`) and polls `/health` for the status dot. `static/css/app.css` is the only stylesheet; `static/vendor/fonts/` holds IBM Plex Sans and Mono (OFL-1.1).

**Key decision.** One process and no build step: the UI is plain HTML, CSS and JavaScript that only calls the existing JSON API, so every rule stays in `app/` and is enforced by the API. The one thing the server resolves is who is looking: the user and project come from the `hld_user` / `hld_project` cookies (set by the top-bar selectors), and `web.py` calls the API's own `list_projects` to get the role, so the review page HTML contains the approve / reject buttons only for reviewers and admins. The graph is the pyvis HTML from `/graph`, made self-contained in `graph.py` (vis-network inlined, CDN tags removed) and shown in a sandboxed iframe.

**Likely questions.**
1. *Is hiding the buttons the access control?* No. `POST /projects/{pid}/review` checks the reviewer role on every call; the page only avoids offering an action that would fail.
2. *Where did the model selector, Evaluation and Audit tabs go?* There is one model, so no selector. Evaluation results are in `eval/results.md`; the audit log is `GET /audit` (admin only), shown with curl or the Swagger page in the demo.
3. *Does the UI work offline?* Yes. `tests/test_web.py` fails if any template, script or stylesheet contains `http://` or `https://`, and the Playwright smoke test fails if the browser requests anything outside the server.
4. *Where is the session question history kept?* In a JavaScript array on the Ask page (last 5); nothing is stored in the browser.

## app/__init__.py

Package marker with a one-line docstring. Nothing to explain.

---

## Questions that cut across modules

1. *What is the single most important number to be honest about?* Real-document extraction precision and recall are not verified; the headline scores on synthetic v1 are an upper bound; v4 is the held-out result and has the caveat that the same AI session produced the generator and the parser changes.
2. *What did you write yourself?* **[AUTHOR]**. See `docs/AI_TOOL_DECLARATION.md`.
3. *What would break first at scale?* Single-process SQLite, in-memory graph, per-section LLM calls, header-based authentication, and hand-tuned diagram thresholds.
4. *Why local models?* Documents are proprietary design material that must not leave the machine; the cost is a 3B model with limited reasoning, which is why the design filters and refuses rather than trusting the model.
