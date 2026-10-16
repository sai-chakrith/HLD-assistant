# AUTOSAR HLD Document Analysis Assistant

**Tata Tech Pulse — Final Project | Case Study 1**
**Owner:** Karthik (solo) | **Timeline:** 4 days | **Status:** Pilot

---

## 1. Problem

AUTOSAR High-Level Design documents describe software components (SWCs), ports, interfaces, signals, and data flows across hundreds of pages of unstructured PDF. Architects, integrators, and testers read them manually. This is slow and inconsistent, and mismatches (missing providers, wrong interface types, orphan signals, naming drift) are easy to miss.

## 2. Primary Users / Stakeholders

- System / AUTOSAR architects
- Software developers
- Integration engineers
- Test engineers

## 3. Solution

A locally runnable, AI-assisted tool that:

1. **Ingests documents under control**: only files labeled *approved* or *synthetic* are accepted, with file-type validation, a project, a version label, and a source status recorded at upload.
2. **Extracts sections, tables, and metadata** (headings, page numbers, document version).
3. **Recognizes architecture entities**: components (SWCs), ports, interfaces, signals.
4. **Maps interfaces and dependencies** into a graph.
5. **Answers questions with citations and a confidence / limitation indicator.**
6. **Generates cited summaries and component reports**, including **functional-flow summaries**.
7. **Compares document revisions** at the entity level (added / removed / changed).
8. **Runs mismatch and completeness checks** (rule-based).
9. **Keeps a human in the loop**: outputs stay *Unreviewed* until a reviewer approves them, and every decision is logged.
10. **Exports** structured results for downstream use (CSV / Excel / JSON).

**Design rule:** the LLM *extracts* and *answers*; consistency checks are **rule-based Python over the graph**, so findings are explainable and not hallucinated.

## 4. Requirement Traceability

Every requirement from the case study brief maps to a module and a deliverable.

### Required functional capabilities

| Requirement | Implementation | Module | Deliverable |
|---|---|---|---|
| Document upload and controlled ingestion | Upload form with project, version, and source-status (approved / synthetic) fields; file-type and duplicate checks; unapproved files rejected | `ingest.py`, `controls.py` | D1 |
| Section, table and metadata extraction | PyMuPDF headings + text, pdfplumber tables, page / section / version metadata | `ingest.py`, `chunk.py` | D1 |
| OCR for scanned pages (in scope) | Pages without a text layer: pymupdf render at 300 dpi + Tesseract (`pytesseract`), word confidences kept; OCR chunks flagged `ocr` / `ocr_confidence`, citations say "from OCR, confidence N" | `ocr.py`, `ingest.py` | D1 |
| Architecture entity recognition | Table parsing + LLM extraction validated with Pydantic | `extract.py` | D3 |
| Interface and dependency mapping | networkx graph of components ↔ ports ↔ interfaces ↔ signals | `graph.py` | D4 |
| Natural-language Q&A with citations | RAG over Chroma, answers cite page + section | `qa.py` | D2 |
| Cited summaries and component reports | Per-component report (purpose, ports, interfaces, dependencies) + document summary, every claim cited | `summarize.py` | D13 |
| Revision comparison | Entity-level diff between versions | `compare.py` | D6 |
| Potential mismatch and completeness checks | Rule engine over the graph and inventory | `checks.py` | D5 |

### Expected outputs

| Output | Where it comes from |
|---|---|
| Component and interface inventory | `extract.py` → SQLite → Architecture screen (inventory tabs) |
| Dependency maps | `graph.py` → pyvis (self-contained HTML) → Architecture screen |
| Functional-flow summaries | `summarize.py` (flow sections + graph paths) → Architecture side panel |
| Searchable Q&A responses with citations | `qa.py` → Ask screen |
| Potential inconsistency report | `checks.py` → Review screen + export |
| Structured export for downstream use | `export.py` → CSV / Excel / JSON |

### Inputs supported

| Input | How it is handled |
|---|---|
| Approved / synthetic AUTOSAR HLD documents | Main ingestion path |
| Interface specifications | Ingested with `doc_type=interface_spec`; tables parsed into interfaces |
| Architecture descriptions | Ingested with `doc_type=architecture`; prose extraction |
| Component catalogues | Ingested with `doc_type=catalogue`; table parsing into components |
| Revision history | Change-history table parsed into the version record; used alongside the entity diff |

### Mandatory controls

| Control | Implementation |
|---|---|
| Use only approved or synthetic documents | Source-status field required at upload; anything else is rejected and logged |
| Project isolation | One Chroma collection + SQLite `project_id` scope per project; queries never cross projects |
| Access control | Simple users table with roles (*viewer*, *reviewer*, *admin*) mapped to projects; enforced in FastAPI dependencies |
| Restrict answers to retrieved evidence | Prompt allows only supplied chunks; answer refused if no chunk passes the similarity threshold |
| Source citations | Page + section on every answer, summary claim, entity, and finding |
| Confidence / limitation indicators | High / Medium / Low badge from retrieval score + citation check; limitation note when evidence is partial or comes from a table parse fallback |
| Human review before reliance | All generated outputs start as *Unreviewed*; only *reviewer* role can approve; exports are watermarked with review status; decisions logged in an audit table |

## 5. Data Strategy

| Source | Purpose |
|---|---|
| **Real:** AUTOSAR "Explanation of Application Interfaces" PDFs (autosar.org) — Body & Comfort (Doc 268), Chassis (Doc 270), HMI/Multimedia/Telematics (Doc 272) | Ingestion realism, cited Q&A, entity extraction, revision comparison (two releases of the same doc) |
| **Synthetic:** small HLD for a fictional ECU (v1 + v2, 8–10 pages) with 5–8 planted defects + `ground_truth.json` | Measurable evaluation of extraction and inconsistency detection |

AUTOSAR PDFs are **not committed** to the repo; `scripts/download_data.py` fetches them from autosar.org (authorized-source principle).

## 6. Architecture

```
Browser UI (HTML/CSS/JS served by FastAPI)  ──JSON API──>  FastAPI services
                           │
        ┌──────────────────┼──────────────────────┐
        v                  v                      v
  Ingestion           RAG / Q&A              Extraction + Checks
  PyMuPDF,            ChromaDB +             Pydantic schemas,
  pdfplumber,         bge-small-en-v1.5,     networkx graph,
  section chunker     cited prompts          rule engine, diff
        │                  │                      │
        └──────────> SQLite (entities, findings, reviews, audit)
                           │
                    Local LLM via Ollama (no API keys, fully offline)
                    Qwen2.5-3B (Q4, fits 4GB VRAM)
                           │
                    Local trace log (JSONL)
```

## 7. Tech Stack

| Layer | Choice |
|---|---|
| Parsing | PyMuPDF, pdfplumber |
| Chunking | Section-aware, heading hierarchy, page + section metadata |
| Embeddings | `BAAI/bge-small-en-v1.5` (CPU) |
| Vector DB | ChromaDB (one collection per project / document version) |
| LLM | Ollama, local only: qwen2.5:3b. No cloud APIs, no API keys |
| Orchestration | Custom Python pipeline (LangChain where convenient) |
| Structured store | SQLite |
| Graph | networkx, pyvis for visualization |
| API | FastAPI |
| UI | FastAPI-served HTML: Jinja2 page shells, one CSS file, vanilla ES modules; vendored fonts; no build step |
| Observability | Local JSONL trace log (prompt, chunks, answer, latency) |
| Deployment | Docker Compose |

## 8. Repository Layout

```
hld-assistant/
  app/
    controls.py    # approved/synthetic gate, users, roles, project isolation, audit log
    ingest.py      # PDF -> text, tables, headings, metadata
    chunk.py       # section-aware chunking
    retrieve.py    # Chroma search (project-scoped)
    llm.py         # local Ollama client, per-request model switch (ALLOWED_MODELS, default qwen2.5:3b)
    qa.py          # cited answers + confidence badge
    extract.py     # entities -> Pydantic -> SQLite
    graph.py       # networkx dependency graph
    summarize.py   # cited document, component, and functional-flow summaries
    checks.py      # mismatch + completeness rules
    compare.py     # entity-level revision diff
    export.py      # CSV / Excel / JSON with review status
    api.py         # FastAPI routes (JSON API) + static files
    web.py         # page routes for the 4 screens
    templates/     # base.html + documents / ask / architecture / review
    static/        # css/app.css, js/*.js, vendor/fonts
  data/synthetic/  (hld_v1.pdf, hld_v2.pdf, ground_truth.json)
  eval/            (gold_questions.json, run_eval.py)
  scripts/download_data.py
  docker-compose.yml
  README.md
```

## 9. Day Plan

| Day | Focus | Done when |
|---|---|---|
| **1** | Download AUTOSAR docs, write synthetic HLD v1/v2 + ground truth, controlled upload (source status, project, version), ingestion, chunking, project-scoped Chroma, cited Q&A with confidence badge | Can upload an approved doc and get a cited answer with a confidence level |
| **2** | Table parsing + LLM extraction, SQLite, networkx graph, mismatch + completeness rules | Inventory + issue list generated for synthetic HLD |
| **3** | Component reports + functional-flow summaries, revision comparison, roles + review workflow, browser UI, export | Full UI flow works end to end with review status on every output |
| **4** | Evaluation script (qwen2.5:3b, local), Docker, README, architecture diagram, demo video | Metrics table in README, demo recorded |

## 10. Key Risks

| Risk | Mitigation |
|---|---|
| Synthetic HLD eats Day 1 | Cap at one ECU, 6–8 components, 8–10 pages |
| Table extraction fails on AUTOSAR PDFs | pdfplumber first, LLM fallback, page citation for review |
| 3B local model weak at extraction | Pydantic validation + retry; table-first extraction; compare both local models and report honestly |
| Hallucinated relationships | Answers restricted to retrieved chunks; checks are rule-based |

## Related Docs
- [scope.md](scope.md) — what is in and out
- [outcomes.md](outcomes.md) — deliverables and success metrics
