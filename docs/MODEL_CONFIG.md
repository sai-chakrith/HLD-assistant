# Model and configuration record

Purpose: every model, library setting, threshold and prompt that influences a result, so a run can be reproduced and
audited. Written against the repository state of 2026-10-07. Items needing the author are marked **[AUTHOR]**.
Everything runs locally; there are no cloud APIs and no API keys in the code or configuration.

## 1. Models

| Role | Model | Ollama tag | Quantisation | Size on disk | Parameters / context | Licence (verify) |
|---|---|---|---|---|---|---|
| LLM (default `OLLAMA_MODEL`, live-demo default `DEMO_MODEL`) | Qwen2.5 3B Instruct | `qwen2.5:3b` (same blob as `qwen2.5:3b-instruct-q4_K_M`) | Q4_K_M | 1,929,912,432 bytes (about 1.8 GiB) | 3.1B parameters, native context 32768 | Qwen Research License (Alibaba Cloud); to my understanding this size is not under Apache-2.0 and the licence restricts commercial use. **[AUTHOR]** confirm from the model card and licence text before any use beyond this assessment |
| Embedder | BAAI/bge-small-en-v1.5 | n/a (Hugging Face, loaded by sentence-transformers) | float32 weights | **[AUTHOR]** (size of `model.safetensors` in the Hugging Face cache) | 384-dimensional vectors, 12 layers, max 512 tokens | MIT |

Sizes, quantisation and context lengths come from `GET /api/tags` of the local Ollama server (version 0.35.1 at the
time of writing). Digest: qwen2.5:3b `357c53fb659c...`.
Embedder revision: local Hugging Face cache snapshot `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`
(`models--BAAI--bge-small-en-v1.5`). The code requests the model by name and takes whatever snapshot is cached
(`app/retrieve.py:52`), so the snapshot hash is the only precise version record.

Embedder behaviour (`app/retrieve.py`): queries are prefixed with `Represent this sentence for searching relevant
passages: ` (line 16), documents are embedded without a prefix, vectors are normalised, device `cpu`, batch size 32.
If the model cannot be loaded, a deterministic hashing embedder (384 dimensions, bag of words) is used and Q&A refuses
to answer, because the thresholds are only valid for bge.

Hardware used for the evaluation: NVIDIA RTX 3050 with 4 GB VRAM (peak 3.2 GB observed), Windows 11, Python 3.13.6.
The brief asked for Python 3.11; only 3.13 was available.

## 2. Vector store and key libraries

| Component | Version in the project venv | Notes |
|---|---|---|
| chromadb | 1.5.9 | `PersistentClient`, anonymised telemetry off, one collection per project, cosine space (`hnsw:space=cosine`), score = 1 - cosine distance (`app/retrieve.py:96-160`) |
| sentence-transformers | 6.1.0 | loads bge-small |
| torch | 2.14.1+cpu | CPU build; the embedder runs on CPU, the GPU is left to Ollama |
| PyMuPDF | 1.28.2 | text, vector drawings (diagram extraction), page rendering for OCR |
| pdfplumber | 0.11.10 | table detection |
| pytesseract + Tesseract binary | 0.3.13 + Tesseract v5.4.0.20240606 (Windows build as installed on the development machine; leptonica 1.84.1) | OCR for scanned pages, 300 dpi |
| FastAPI / uvicorn / Streamlit | 0.142.2 / 0.54.0 / 1.65.0 | service and UI |

Full list with licences: `docs/CITATIONS.md`.

## 3. Ollama call options (`app/llm.py`)

Endpoint `POST {OLLAMA_BASE_URL}/api/generate`, non-streaming. Request payload: `model`, `prompt`, `system`,
`stream: false`, `options`, and `format` (a JSON schema) when structured output is requested.

| Option | Value | Where |
|---|---|---|
| `temperature` | 0.1 | `app/llm.py:27` (`OPTIONS`) |
| `num_ctx` | 4096 | `app/llm.py:27` |
| `seed` | not set (Ollama default). Repeatability comes from the response cache, not from a fixed seed | n/a |
| `top_p`, `top_k`, `num_predict`, `repeat_penalty` | not set (model / Ollama defaults) | n/a |
| `keep_alive` | not set on normal calls (Ollama default); `30m` on the warm-up call only | `app/api.py:43` |
| Request timeout | 180 s | `app/llm.py:29` |
| Schema validation retries | 2 retries after the first attempt (3 attempts), then `ValueError` | `app/llm.py:28,211-224` |
| Model allow-list | `ALLOWED_MODELS` (default `qwen2.5:3b`); other names raise `ModelNotAllowed` | `app/llm.py:45-50` |

Response cache: table `llm_cache` in the SQLite database, key = sha256 of `[model, system, prompt, schema]`. Every call
(hit or miss) is appended to `data/traces.jsonl` with timestamp, model, purpose, prompt hash, chunk ids, latency, cached
flag and outcome. Because sampling is not seeded, uncached calls at temperature 0.1 can differ between runs; cached
calls repeat exactly.

## 4. Settings (`app/config.py`, overridable by environment or `.env`)

| Setting (env var) | Default | Meaning |
|---|---|---|
| `DATA_DIR`, `RAW_DIR`, `PROCESSED_DIR`, `SYNTHETIC_DIR` | `data`, `data/raw`, `data/processed`, `data/synthetic` | file locations |
| `CHROMA_DIR` | `chroma` | vector store directory |
| `DB_PATH` | `data/hld.db` | SQLite (entities, findings, reports, answers, audit log, LLM cache) |
| `TRACE_PATH` | `data/traces.jsonl` | LLM call trace |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | embedder name |
| `LLM_PROVIDER` | `ollama` | `ollama` or `none`; cloud names are rejected |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | |
| `OLLAMA_MODEL` | `qwen2.5:3b` | default model for scripts and evaluation |
| `DEMO_MODEL` | `qwen2.5:3b` | model the API uses for interactive requests that name no model; also the warm-up model |
| `ALLOWED_MODELS` | `qwen2.5:3b` | |
| `SIMILARITY_THRESHOLD` | 0.65 | Q&A refuses when the best retrieval score is below this |
| `HIGH_SCORE` | 0.75 | top score needed (with other conditions) for High confidence |
| `FUZZY_THRESHOLD` | 88 | rapidfuzz ratio for near-duplicate names (check M4) and possible renames in the entity diff |
| `CHUNK_TARGET_TOKENS` / `CHUNK_MAX_TOKENS` / `CHUNK_OVERLAP_TOKENS` | 500 / 600 / 50 | chunking; tokens are estimated as 1.3 per word (`app/chunk.py:11`); a chunk never spans two sections; tables become separate markdown chunks |
| `DIAG_MIN_BOX` | 22 | minimum box size (pt) to count as a component box |
| `DIAG_JOIN_TOL` | 1.5 | tolerance (pt) for chaining line segments into a path |
| `DIAG_TOUCH_TOL` | 4 | max distance (pt) for an arrow end to count as touching a box border |
| `DIAG_HIGH_DIST` | 3 | gap (pt) between a label or arrow end and a box border at or below which the entity gets High confidence |
| `DIAG_LABEL_ARROW_DIST` | 9 | max distance (pt) from a signal label to its arrow |
| `DIAG_HIGH_LABEL_DIST` | 7 | distance (pt) from a signal label to its arrow at or below which the signal can get High confidence |
| `DIAG_PORT_INSIDE_MARGIN` | 20 | a port label inside a box counts when within this margin of the border |
| `DIAG_PORT_ASYM` | 8 | difference (pt) between the left and right gap of a text line inside a box above which it is read as a port label (used when no reference font size is available) |
| `DIAG_PORT_OUTSIDE_DIST` | 16 | a port label outside a box counts when within this distance of the border |
| `DIAG_PORT_ARROW_Y` | 6 | vertical tolerance (pt) for matching a port label to an arrow end (`app/diagram.py:469`) |
| `LLM_EXTRACT_MIN_CHARS` | 250 | sections shorter than this (after removing figure labels) are not sent to the LLM |
| `LLM_EXTRACT_CHUNK_CHARS` | 3000 | section text per LLM extraction call |
| `LLM_EXTRACT_MODEL` | empty (provider default) | model override for extraction |
| `OCR_ENABLED` | true | OCR for pages without a text layer |
| `TESSERACT_CMD` | empty (PATH, then the default Windows path) | |
| `WARMUP` (read in `app/api.py`, not in `Settings`) | 1 | `0` skips the start-up warm-up |
| `EMBEDDER` (environment only) | unset | `hash` forces the hashing embedder |
| `ALLOW_HASH_QA` (environment only) | unset | `1` lets Q&A run on the hashing embedder (unit tests only) |

The diagram thresholds were tuned by hand against the five real PDFs (`docs/data_notes.md`).

### Constants in code (not configurable by environment)

| Constant | Value | Where |
|---|---|---|
| Retrieval depth `k` | 5 chunks (API field `k`, default 5) | `app/api.py:230`, `app/qa.py:251` |
| Refusal text | `I cannot answer this from the project documents.` | `app/qa.py:34` |
| Confidence cut-offs | share of sentences cited: Low below 0.5, High at 0.8 or more | `app/qa.py:36` |
| Lexical-support minimum for report sentences | 0.4 | `app/summarize.py:46` (`SUPPORT_MIN`) |
| Evidence quote limit (LLM extraction) | 25 words, must occur verbatim in the section | `app/extract.py:537` |
| Document-summary piece size / max pieces per section | 1800 chars / 3 | `app/summarize.py:47,282` |
| OCR resolution / text-layer threshold | 300 dpi / fewer than 20 characters plus at least one image = scanned | `app/ocr.py:22-23,31` |
| Section diff fuzzy title match | 85 | `app/compare.py:213` (`SEC_FUZZY`) |
| Section diff caps | 12 sentences and 20 table rows per change | `app/compare.py` (`MAX_SENTENCES`, `MAX_ROWS`) |

## 5. Where each prompt is used

| Prompt | Function | Output handling |
|---|---|---|
| A.1 + A.2 | `qa.ask` | free text; sentences without a valid `[Cn]` label are removed (`qa.postprocess`) |
| A.3 + A.4 (+ A.5) | `extract.extract_from_prose` | JSON conforming to `ProseExtraction`; items kept only if the quote occurs verbatim in the section |
| A.6 + A.7 | `summarize.component_report` | sentences must carry a valid `[Cn]` or `[Fn]` label and share at least 40% of content words with the cited text |
| A.8 + A.9 | `summarize.document_summary` (map) | same filter with `[Sn]` labels |
| A.10 + A.11 | `summarize.document_summary` (reduce) | same filter, at most 6 sentences |
| A.12 | API warm-up | response ignored |

The checks (M1-M4, C1-C4), the dependency graph, the entity diff and the section diff use no LLM and no prompt.

## Appendix A. Prompts (exact text, copied from the code at build time of this document)

The system prompts below are the resolved Python string values (adjacent string literals concatenated, f-string fields substituted). Template lines marked `{...}` are filled at run time. Line numbers refer to the commit this document was written against.

### A.1 Q&A system prompt

`app/qa.py:38-43 (SYSTEM_PROMPT; NOT_STATED defined at app/qa.py:35)`

```text
You answer questions about technical architecture documents using ONLY the evidence chunks provided, which are labelled [C1], [C2], ... Rules: 1) Write 1-4 short sentences. 2) End EVERY sentence, including the first, with the chunk label(s) it comes from, written exactly like [C1] or [C1, C3]; never put component names in brackets. 3) If the evidence does not cover the question or part of it, write exactly: Not stated in the provided documents. 4) No outside knowledge. Example: The FooManager controls the lamp [C2]. It reads the switch state [C1, C3].
```

### A.2 Q&A user prompt template

`app/qa.py:114-122 (build_prompt)`

```text
EVIDENCE:
[C1] (<doc_id> v<version>, p.<page_start>, section <section_id> <section_title>)
<chunk text>

[C2] (...)
...

QUESTION: {question}

ANSWER (every sentence ends with a label like [C1]):
```

### A.3 Entity extraction system prompt (LLM prose layer)

`app/extract.py:502-512 (PROSE_SYSTEM)`

```text
You extract AUTOSAR software architecture entities from ONE section of a High Level Design document.
Return JSON with lists: components (software components / compositions / actuators / sensors), ports (name, component = the owner, direction = provided if the component offers/provides/sends it, required if it requires/consumes/receives it, interface = its interface name), interfaces (name, kind), signals (name, sender component, receivers = list of components).
Rules: list ONLY entities that the text explicitly names. Never invent names. Use the exact spelling of the text. For every item give an evidence quote of at most 25 words copied word for word from the text. If the text names no such entity, return empty lists. Do not list generic words like 'system' or 'driver'.
```

### A.4 Entity extraction user prompt template

`app/extract.py:617`

```text
Section: {section title}

Text:
{chunk of section text, <= LLM_EXTRACT_CHUNK_CHARS characters}
```

### A.5 Schema-retry suffix (appended when JSON fails validation; up to 2 retries)

`app/llm.py:212-217 (_generate_validated)`

```text
{original prompt}

Your previous reply was invalid: {validation error, first 500 characters}
Reply again with valid JSON matching the schema only.
```

### A.6 Component report system prompt

`app/summarize.py:129-133 (COMP_SYSTEM)`

```text
You write the purpose of one software component for an architecture review, using ONLY the labelled evidence given: chunks [C1].. and facts [F1].. . Rules: 1) Write 2-4 short sentences. 2) End EVERY sentence with the label(s) it comes from, like [C1] or [F2, C1]. 3) No outside knowledge; do not invent names. 4) If the evidence says little, write fewer sentences. Example: The FooManager controls the lamp [C2]. It has 3 ports [F3].
```

### A.7 Component report user prompt template

`app/summarize.py:175-176 (_component_purpose)`

```text
EVIDENCE CHUNKS:
[C1] (p.<page>, section <id> <title>)
<first 700 chars of chunk>

[C2] ...

FACTS:
[F1] <fact>
[F2] <fact>
...

TASK: Describe the purpose of {component name} (every sentence ends with a label).
```

### A.8 Document summary, map step system prompt

`app/summarize.py:256-259 (MAP_SYSTEM)`

```text
You summarise one passage of a technical architecture document using ONLY the passage. Write 2-3 bullet points, one sentence each, and end EVERY bullet with the passage label like [S1]. No outside knowledge. Example: - The FooManager controls the lamp [S1].
```

### A.9 Document summary, map step user prompt template

`app/summarize.py:313-315 (document_summary)`

```text
PASSAGE [S{n}] (section {section_id} {title}, p.{page_start}):
{piece of section text, <= 1800 chars}

BULLETS (each ends with [S{n}]):
```

### A.10 Document summary, reduce step system prompt

`app/summarize.py:260-263 (REDUCE_SYSTEM)`

```text
You write a short overview (3-5 sentences) of a document from labelled bullet points. Use ONLY the bullets. End EVERY sentence with the label(s) of the bullets it is based on, like [S2] or [S2, S5]. Do not invent labels or facts.
```

### A.11 Document summary, reduce step user prompt template

`app/summarize.py:335-336 (document_summary)`

```text
BULLETS:
- {bullet text}
- ...

OVERVIEW (every sentence ends with a label):
```

### A.12 API warm-up request (not a prompt template; no system prompt)

`app/api.py:42-44 (_warmup)`

```text
POST /api/generate {"model": DEMO_MODEL, "prompt": "hi", "stream": false, "keep_alive": "30m", "options": {"num_predict": 1}}
```

Constants used by the answer filter: refusal text `I cannot answer this from the project documents.` (app/qa.py:34); evidence-only text `LLM not configured — showing top retrieved evidence` (app/qa.py:37).
