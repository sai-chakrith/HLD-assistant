# Local upgrade verification

Verified on 15 October 2026 in this Windows checkout, with Python 3.12.14. The app is running at http://127.0.0.1:8000.

## What changed

- Extraction retains table-row evidence, continued-table row pages, page-specific prose text, and diagram label bounding boxes. Functional descriptions can identify additional named components. Diagram directions now recognise open chevrons and closed port glyphs; names and prefixes do not substitute for that geometry. Unresolved diagram directions have reduced confidence.
- LLM extraction requires a non-empty evidence field in its generation schema. Every accepted quote must occur in the current source chunk and contain the entity's complete name. Related owners/interfaces/endpoints must occur in that quote, and descriptions cannot introduce unsupported model-written facts.
- Q&A removes cited claims that fail its evidence checks. Explicit sender/receiver, provided/required, identifier, and number checks catch several relationship reversals that word overlap alone accepted. These checks remain conservative heuristics, not a general entailment proof.
- Architecture now has reviewer field correction, approve, and reject controls. Overrides, reasons, and review history survive extraction. Changed machine facts require another review. Reviewed rows no longer found by extraction remain visible with a warning, but are excluded from reasoning. Unchanged findings retain their IDs and decisions across check reruns. Rejected/inactive facts are excluded from new graphs, inventory-backed answers, reports, checks, and revision comparisons. Stored reports remain historical snapshots.
- Re-extraction reuses retained uploaded PDFs, so uploaded diagram facts are not silently lost. Inventory coverage distinguishes an extraction gap from evidence that an element is absent.
- Optional access-token authentication uses hashed tokens, bearer headers, and HttpOnly browser cookies. In that mode, demo headers/cookies cannot impersonate another account. Default operation remains a localhost demo with seeded users. Enterprise identity management is outside this pilot.
- `start.ps1` starts/checks the local runtime, verifies embedding/model availability, and launches the app. `-Setup` uses the pinned dependency lock; `-EvidenceOnly` offers structural features without claiming generated answers. Concurrent startup/health requests now load the embedding model once.

## Verification completed

- Preserved original code: **455 passed**, one optional browser test skipped, eight model tests deselected. This baseline used the newly installed dependency environment, so it is a source comparison, not a reproduction of the old machine setup.
- Final regression suite: **482 passed**, including the Chromium browser test; eight real-model tests deselected in that run.
- Separate real-model suite: **8 passed**. These cover actual generation, schema/cache behaviour, grounded Q&A/refusal, prose extraction, and reports/summaries.
- Running-app structural checks: **12/12 passed**, covering BGE search, all four pages, seven seeded documents, inventory, graph, planted findings, comparison, Excel export, and access denial.
- Live API examples: **10/10 passed**: seven questions (including a refusal) and component, flow, and document reports. Examples include a cited MotorDriver answer, MotorCommand sender/receiver, and the RainSensor's provided StaRainAmount port. These are smoke checks with explicit expected terms and citations, not a new answer-accuracy benchmark.
- Offline software check: a fresh, uncached BGE/Ollama answer passed with Python non-loopback socket connections blocked. This is not a physical network-disconnection test and does not instrument every native library.
- The new launcher successfully started the final app. Final health reports BGE without fallback, both configured models, warm-up ready, and `qa_ready=true`.

The suite emits deprecation warnings from the installed Starlette/httpx test-client integration. No tests failed in the final runs.

## Existing development-reference comparison

The same 51 verified reference rows and the same public PDF files were used before and after. No reference labels or frozen v4 files were changed.

- A port counts as a joint match only when one prediction matches its name, owner, and annotated direction. Joint port matches improved from **2/15 to 11/15**.
- Component page-scoped F1 improved from approximately **0.595 to 0.664**; 15 of 22 reference component names are now found.
- Port name F1 remains approximately **0.937**. Name-only performance obscured the earlier direction problem, which is why joint matching is now reported separately.
- Interface page-scoped F1 remains approximately **0.667**, with 5 of 10 reference interface names found.

This reference is small and not exhaustive. Page-scoped precision is not exhaustive document precision. These are development results, not independent acceptance or evidence of time saved.

## Model and runtime provenance

BGE is loaded from `data/models/bge-small-en-v1.5`. Ollama 0.40.4 runs locally from `tools/ollama` with CPU/Vulkan libraries and detected the laptop's NVIDIA GPU.

The configured model names are local aliases for these verified imports:

- `qwen2.5:3b`: Qwen's `Qwen/Qwen2.5-3B-Instruct-GGUF`, Q4_K_M.
- `qwen3:4b-instruct`: `unsloth/Qwen3-4B-Instruct-2507-GGUF`, Q4_K_M, a community quantisation of the Qwen model.

Exact repository revisions, byte sizes, and verified SHA-256 hashes are in `eval/upgrade/model_provenance.json`. These imports differ from earlier saved deployments, so historical model scores in `eval/results.md` are not new results for these imports. Public source PDFs were downloaded with certificate verification enabled. Failed/intermediate model downloads were removed after verification; the verified GGUFs and runtime blobs remain local.

## Remaining limits

- Independent evaluation on authorised, representative engineering HLDs and a counterbalanced reviewer study still require real document owners and reviewers. See `INDEPENDENT_EVALUATION.md` and `review_effort_protocol.md`. No independent approval, reviewer identity, acceptance threshold, or time-saving number has been invented.
- Extraction remains incomplete, particularly for some compositions, interfaces, ambiguous ownership, and raster diagrams. A clean findings list does not establish architectural correctness.
- Human approval is still necessary. Citation/evidence checks reduce specific failure modes but do not prove every statement semantically correct.
- Docker was not built: no Docker executable was available in this environment. The verified delivery is the Windows local runtime.

## Run and inspect

Run `./start.ps1` from the project directory, then open http://127.0.0.1:8000. On Architecture, select an entity's **Review / edit** button to correct fields and record a reason. Enable token authentication before shared use as described in the README.

The compact evidence bundle is in `eval/upgrade/`: test results, live checks, before/after extraction audit, model provenance, and a code patch. Detailed setup logs, the original code snapshot, and the pre-refresh database/processed-file backup remain in `eval/_work/upgrade/`. The original checkout had no Git metadata; no commit, push, or pull request was created.
