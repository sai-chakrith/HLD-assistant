# Citations, sources and licences

Versions are those installed in the project virtual environment (`pip show`, 2026-10-07). Licences are the `License` /
`License-Expression` / trove classifier values reported by the installed package metadata unless marked otherwise. Entries
marked **[AUTHOR verify]** were not confirmable from the installed metadata and must be checked against the upstream licence
text before submission.

## 1. Documents

The five real documents are public downloads from autosar.org, fetched on the user's machine by `scripts/download_data.py`
into `data/raw/`. They are **not committed to the repository and not copied into Docker images**.

| doc_id | Title | Release | URL |
|---|---|---|---|
| `bodycomfort_r3.1.5` | Explanation of Application Interfaces of the Body and Comfort Domain | AUTOSAR R3.1.5 (Classic Platform) | https://www.autosar.org/fileadmin/standards/R3.1.5/CP/AUTOSAR_ApplicationInterfaces_Explanation_BodyComfort.pdf |
| `bodycomfort_r3.2.3` | Explanation of Application Interfaces of the Body and Comfort Domain | AUTOSAR R3.2.3 (Classic Platform) | https://www.autosar.org/fileadmin/standards/R3.2.3/CP/AUTOSAR_ApplicationInterfaces_Explanation_BodyComfort.pdf |
| `chassis_r3.1.5` | Explanation of Application Interfaces of the Chassis Domain | AUTOSAR R3.1.5 (Classic Platform) | https://www.autosar.org/fileadmin/standards/R3.1.5/CP/AUTOSAR_ApplicationInterfaces_Explanation_Chassis.pdf |
| `chassis_r3.2.3` | Explanation of Application Interfaces of the Chassis Domain | AUTOSAR R3.2.3 (Classic Platform) | https://www.autosar.org/fileadmin/standards/R3.2.3/CP/AUTOSAR_ApplicationInterfaces_Explanation_Chassis.pdf |
| `hmi_r25-11` | Explanation of Application Interfaces of the HMI, Multimedia and Telematics Domain | AUTOSAR R25-11 (Classic Platform) | https://www.autosar.org/fileadmin/standards/R25-11/CP/AUTOSAR_CP_EXP_AIHMIMultimediaAndTelematics.pdf |

Publisher and copyright: AUTOSAR (AUTOSAR Development Partnership), https://www.autosar.org. The documents carry a legal
disclaimer on page 3 and the footer text "AUTOSAR CONFIDENTIAL" on every page of the downloaded copies. **[AUTHOR]** read the
disclaimer and state the terms under which you used them (public download, internal evaluation, no redistribution) and the
access date for each download (`data/raw/manifest.json` records the sha256 of the files fetched, if present on your machine).

Synthetic documents (`data/synthetic/`): written for this project (Window Lift ECU v1, v2, v3; Door Control ECU v4; a scanned
sample); no third-party content. Licence of these files: **[AUTHOR]** choose (for example "created by the author, no restriction").

## 2. Models

| Model | Source | Licence | Notes |
|---|---|---|---|
| Qwen2.5 3B Instruct (`qwen2.5:3b`, Q4_K_M) | Alibaba Cloud (Qwen team), through the Ollama library, https://ollama.com/library/qwen2.5 | Qwen Research License (to my understanding; this size is not Apache-2.0) | **[AUTHOR verify]**; the research licence restricts commercial use |
| BAAI/bge-small-en-v1.5 | Beijing Academy of Artificial Intelligence, https://huggingface.co/BAAI/bge-small-en-v1.5 | MIT | Citation to give if asked: Xiao et al., "C-Pack: Packaged Resources To Advance General Chinese Embedding", 2023 (the bge family paper). **[AUTHOR verify]** the citation form you prefer |
| Ollama (runtime for the LLMs, version 0.35.1 on the development machine) | https://ollama.com, https://github.com/ollama/ollama | MIT **[AUTHOR verify]** | local inference server, not bundled with this repository |

## 3. Software libraries

| Library | Version | Licence | Used for |
|---|---|---|---|
| PyMuPDF (`pymupdf`) | 1.28.2 | Dual licensed: GNU AGPL 3.0 or Artifex commercial licence (see the flag below) | text, drawings, page rendering |
| pdfplumber | 0.11.10 | MIT | table detection |
| Tesseract OCR (binary) | 5.4.0.20240606 (installed here) | Apache-2.0 | OCR engine for scanned pages; not a Python package, installed separately (UB Mannheim Windows build). **[AUTHOR verify]** the build you ship or document |
| pytesseract | 0.3.13 | Apache-2.0 | Python wrapper for Tesseract |
| Pillow | 12.3.0 | MIT-CMU (HPND style, permissive) | image handling for OCR (dependency) |
| chromadb | 1.5.9 | Apache-2.0 (trove classifier "Apache Software License") | vector store |
| sentence-transformers | 6.1.0 | Apache-2.0 | loads bge-small |
| torch | 2.14.1+cpu | BSD-3-Clause style (package metadata lists a combined expression including Apache-2.0 and BSD). **[AUTHOR verify]** | tensor runtime for the embedder |
| transformers | 5.19.0 | Apache-2.0 | dependency of sentence-transformers |
| numpy | 2.5.3 | BSD-3-Clause (metadata lists additional bundled licences) | |
| FastAPI | 0.142.2 | MIT | REST API |
| uvicorn | 0.54.0 | BSD-3-Clause | ASGI server |
| httpx | 0.28.1 | BSD-3-Clause | HTTP client (listed in requirements) |
| python-multipart | 0.0.32 | Apache-2.0 | file upload parsing |
| Jinja2 | 3.1.6 | BSD-3-Clause | page templates for the browser UI |
| requests | 2.34.2 | Apache-2.0 | HTTP calls to Ollama and the API |
| pydantic | 2.13.5 | MIT | schemas and LLM output validation |
| python-dotenv | 1.2.4 | BSD-3-Clause | `.env` loading |
| rapidfuzz | 3.14.6 | MIT | fuzzy matching (headers, names, titles) |
| networkx | 3.6.1 | BSD-3-Clause | dependency graph |
| pyvis | 0.3.2 | BSD | graph visualisation; it bundles vis-network 9.1.2, which the UI inlines into the graph page (vis-network file header: dual licensed Apache-2.0 or MIT) |
| reportlab | 5.0.1 | BSD | synthetic PDFs, report PDF |
| pandas | 3.0.6 | BSD-3-Clause | tables in export |
| openpyxl | 3.1.5 | MIT | Excel export |
| matplotlib | 3.11.2 | Matplotlib licence (Python Software Foundation based) | evaluation charts |
| pytest | 9.1.1 | MIT | tests |
| playwright (optional, dev only) | 1.63.0 | Apache-2.0 | browser smoke test and screenshots; not in requirements.txt, the test skips without it |
| IBM Plex Sans and IBM Plex Mono (fonts) | npm `@ibm/plex-sans` 1.1.0, `@ibm/plex-mono` 1.1.0 (woff2) | SIL Open Font License 1.1 (licence text in `app/static/vendor/fonts/LICENSE-IBM-Plex.txt`) | UI fonts, vendored in `app/static/vendor/fonts/` so the UI needs no network |
| Mermaid CLI (`mmdc`) | not installed here | MIT **[AUTHOR verify]** | optional, renders the report diagrams (`scripts/build_report_pdf.py`) |

### Licence implication to flag: PyMuPDF is AGPL-3.0 (or commercial)

PyMuPDF is offered under the GNU Affero General Public License v3.0 or an Artifex commercial licence. The AGPL requires that if
the software is modified and made available to users over a network, the corresponding source code of the combined work must be
offered to those users, and the project as a whole would have to be licensed compatibly. For a local, single-user assessment
submission this is unlikely to bite, but it matters if the assistant were deployed as a service, shipped to a customer or
combined into a proprietary product. Options: keep the project open source under AGPL-compatible terms, buy the Artifex commercial
licence, or replace PyMuPDF (pdfplumber, built on pdfminer.six, handles text and tables but not the vector-drawing analysis used for
diagrams, so the diagram extractor and OCR rendering would need a replacement). **[AUTHOR]** state which option you take and whether
the repository carries a licence file (none is present in the repository at the time of writing).

## 4. Tools and services used while building

See `docs/AI_TOOL_DECLARATION.md`. Python 3.13.6; Docker (compose file validated only); Git.

## 5. How to cite this project

**[AUTHOR]** add the form your institution requires (author, title, year, repository link or archive).
