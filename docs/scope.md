# Scope — AUTOSAR HLD Document Analysis Assistant

Scoped to a **4-day solo pilot**, mapped to the case study's *Pilot* phase, with selected *Validation* items.

---

## In Scope

### Controlled Ingestion
- PDF upload restricted to documents labeled **approved** or **synthetic**; anything else is rejected and logged
- Required upload fields: project, document type, version label, source status
- File-type and duplicate checks
- Supported document types: HLD, interface specification, architecture description, component catalogue (revision history parsed from change-history tables)

### Section, Table and Metadata Extraction
- Text, tables, headings, page numbers
- **OCR for scanned pages** (pages with no text layer are rendered at 300 dpi and read with Tesseract; OCR chunks and citations carry `ocr` / `ocr_confidence` and a limitation note)
- Section-aware chunking with metadata: `project_id`, `doc_id`, `doc_type`, `version`, `page`, `section`

### Project Isolation and Access Control
- One Chroma collection and SQLite scope per project; no cross-project retrieval
- Simple users table with roles: *viewer* (read), *reviewer* (approve/reject), *admin* (upload, manage)
- Role checks enforced in FastAPI

### Cited Q&A
- Natural-language questions over ingested documents
- Answers restricted to retrieved evidence; refused if nothing passes the similarity threshold
- Every answer cites page and section
- **Confidence / limitation indicator** (High / Medium / Low) on every answer, with a note when evidence is partial

### Cited Summaries and Component Reports
- Document-level summary with citations
- **Component report** per SWC: purpose, ports, interfaces, upstream/downstream dependencies, source pages
- **Functional-flow summaries** from flow sections and graph paths, every step cited

### Structured Extraction
- Entities: **components (SWCs), ports (provided / required), interfaces, signals**
- Deterministic table parsing first; LLM extraction for prose, validated with Pydantic
- Every entity stores its source page
- Stored in SQLite

### Interface and Dependency Mapping
- networkx graph of components ↔ ports ↔ interfaces ↔ signals
- Graph visualization in the UI (pyvis), filterable by component

### Mismatch and Completeness Checks (rule-based)
**Mismatch checks**
1. Required port with no provider
2. Provider / receiver interface type mismatch
3. Signal with sender but no receiver (or vice versa)
4. Near-duplicate component names (naming drift)

**Completeness checks**
5. Component defined but absent from functional flows
6. Port with no interface assigned
7. Interface referenced but never defined
8. Component missing a description or owner section

Each finding carries rule ID, severity, evidence pages, and review status.

### Revision Comparison
- Entity-level diff between two versions: added / removed / changed
- Applied to synthetic v1 vs v2 and two AUTOSAR releases of the same document

### Human Review
- All generated outputs (entities, findings, summaries) start as **Unreviewed**
- Only the *reviewer* role can approve / reject / correct
- Decisions logged in an audit table with user, timestamp, and comment

### Structured Export
- Inventory, dependency map, findings, summaries, and diff as CSV / Excel / JSON
- Every exported row carries its review status and source citation

### Platform
- Local LLM only via Ollama (qwen2.5:3b). No cloud APIs, no API keys; runs fully offline
- FastAPI backend, Streamlit UI
- Local trace log (JSONL) of prompts, retrieved chunks, answers and latency
- Docker Compose setup

### Evaluation
- Gold set of ~20 questions + entity ground truth for the synthetic HLD
- Metrics run for both local models

---

## Out of Scope (with reason)

| Item | Reason | Scale path |
|---|---|---|
| Enterprise IAM / SSO | Pilot uses a simple users-and-roles table | Integrate enterprise IAM (OIDC / SSO) |
| Neo4j graph store | networkx is enough for pilot scale | Migrate graph to Neo4j |
| Milvus / Weaviate | Chroma is sufficient for pilot | Swap vector store behind `retrieve.py` |
| Kubernetes | Docker Compose is enough | K8s manifests |
| Diagram / image understanding | Needs vision models beyond 4GB VRAM | Vision LLM for figures |
| ARXML ingestion | Case study asks for HLD documents | Add ARXML parser as a source |
| Automatic approval of architecture decisions | Explicitly excluded by case study | — |
| Editing source HLD documents | Explicitly excluded by case study | — |
| Real OEM / Tier-1 HLDs | Proprietary, not available | Plug in once authorized |

---

## Assumptions
- Input PDFs are digitally generated (not scanned).
- AUTOSAR PDFs are downloaded from autosar.org and not redistributed.
- Local inference runs on a 4GB-VRAM GPU using 3B quantized models.
- All AI output is advisory; a human reviews findings before use.

## Constraints
- Solo developer, 4 days
- 4GB VRAM
- No access to proprietary automotive documents
