# Evaluation results

Generated 2026-10-07 21:29 | models: qwen2.5:3b, qwen3:4b-instruct | all local (Ollama, RTX 3050 4 GB) | wall time 89 min. Gold sets were not used for development (dev set: eval/dev_results.md).

## Protocol and decision rule (written before the models were run)

- Same retrieval (hybrid default), prompts, documents, thresholds and machine (RTX 3050 4 GB) for both models.
- The LLM cache is keyed by model, so the models never share answers. Each model runs the whole evaluation twice with cache reads off (LLM_CACHE_READ=0): run 1 starts after unloading every model (cold start measured), run 2 right after (warm). Quality numbers come from run 1; latency from run 2; answers that changed between the runs are listed (sampling noise, temperature 0.1).
- Extraction is run once per model and run on BodyComfort R3.2.3, HMI R25-11 and the frozen held-out v4 document (sha256 checked against eval/heldout_v4.lock before anything runs). The deterministic (no LLM) baseline runs once.
- Real-document precision is page-scoped: extracted entities on the pages that carry gold rows of that type (eval/real_gold.csv is not exhaustive), so it is a lower bound.

Decision rule:

Switch the default model from qwen2.5:3b to qwen3:4b-instruct only if ALL of:
1. (gold Q&A answer correctness of the candidate >= baseline + 8 points, OR mean real-gold extraction F1 >= baseline + 0.05; mean over the entity types whose F1 is defined), measured on the first (cold) run;
2. refusal accuracy on the unanswerable gold questions does not drop;
3. Q&A end-to-end latency p95 of the candidate (second, warm run) stays under 20 s;
4. the candidate fits in the 4 GB GPU without heavy CPU spill: `ollama ps` shows >= 90% of the model on the GPU.
Otherwise keep the baseline. One question = 4 points; differences of 4 points or less are 'no real difference'.

## Decision

**Default model: qwen2.5:3b** (no switch).
Q&A correctness qwen3:4b-instruct - qwen2.5:3b = +20 points (better); real-gold mean F1 -0.01; refusal accuracy not lower; qwen3:4b-instruct Q&A p95 9.3 s; on GPU 67%.
Failed conditions: fits_gpu.

## A. Real-document extraction (51 human-verified rows, eval/real_gold.csv)

BodyComfort R3.2.3 and HMI R25-11, one extraction per model. Recall over gold rows of that type; precision page-scoped (lower bound).

| entity type | deterministic (no LLM) | qwen2.5:3b | qwen3:4b-instruct |
|---|---|---|---|
| component (n=22) | P 0.57 / R 0.55 / F1 0.56 | P 0.65 / R 0.68 / F1 0.66 | P 0.68 / R 0.73 / F1 0.71 |
| port (n=15) | P 0.94 / R 0.93 / F1 0.94 | P 0.94 / R 0.93 / F1 0.94 | P 0.81 / R 0.93 / F1 0.87 |
| interface (n=10) | P 1.00 / R 0.50 / F1 0.67 | P 1.00 / R 0.50 / F1 0.67 | P 1.00 / R 0.50 / F1 0.67 |
| signal (n=4) | P n/a / R 1.00 / F1 n/a | P n/a / R 1.00 / F1 n/a | P n/a / R 1.00 / F1 n/a |
| mean F1 (types with defined F1) | 0.72 | 0.76 | 0.75 |

## B. Held-out v4 extraction and planted defects (frozen; sha256 verified against eval/heldout_v4.lock)

| entity type | deterministic (no LLM) | qwen2.5:3b | qwen3:4b-instruct |
|---|---|---|---|
| components | P n/a / R 0.00 / F1 0.00 | P 1.00 / R 0.50 / F1 0.67 | P 1.00 / R 0.88 / F1 0.93 |
| ports | P 1.00 / R 0.88 / F1 0.93 | P 1.00 / R 1.00 / F1 1.00 | P 1.00 / R 1.00 / F1 1.00 |
| interfaces | P 1.00 / R 1.00 / F1 1.00 | P 1.00 / R 1.00 / F1 1.00 | P 1.00 / R 1.00 / F1 1.00 |
| signals | P 1.00 / R 1.00 / F1 1.00 | P 1.00 / R 1.00 / F1 1.00 | P 0.50 / R 1.00 / F1 0.67 |
| micro_all | P 1.00 / R 0.73 / F1 0.84 | P 1.00 / R 0.89 / F1 0.94 | P 0.86 / R 0.97 / F1 0.91 |
| planted defects | found 4/6, missed M1, C4, FP 0 | found 5/6, missed C4, FP 4 | found 5/6, missed C4, FP 13 |

- qwen2.5:3b defect false positives: C4:DoorLockCtrl, C4:CrashUnlockMon, C4:ChildLockHandler, C4:WindowInhibitMgr
- qwen3:4b-instruct defect false positives: M3:PpFobCmd, M3:PpChildLock, M3:RpImpact, M3:RpChildLock, M3:RpLockState, M3:RpChildLocks, C4:DoorLockCtrl, C4:DoorPosSensor, C4:LockActuatorDrv, C4:KeyFobReceiver, C4:WindowInhibitMgr, C4:CrashUnlockMon, C4:ChildLockHandler

## C. Q&A (25 gold questions: 12 real, 8 synthetic, 5 unanswerable; run 1)

| metric | qwen2.5:3b | qwen3:4b-instruct |
|---|---|---|
| answer correctness (all key facts present) | 75% | 95% |
| key-fact recall | 83% | 97% |
| citation accuracy (doc + page) | 100% | 95% |
| citation accuracy (section) | 85% | 85% |
| groundedness (claim sentences cited) | 83% | 91% |
| refusal accuracy (5 unanswerable) | 100% | 100% |
| false-refusal rate (20 answerable) | 0% | 0% |
| calibration: accuracy when High | 4/5 = 80% | 5/6 = 83% |
| calibration: accuracy when Medium | 10/13 = 77% | 12/12 = 100% |
| calibration: accuracy when Low | 6/7 = 86% | 7/7 = 100% |
| answers changed in run 2 (sampling noise) | R04 (ok->ok), R11 (ok->ok) | R04 (ok->ok), R11 (ok->ok) |
| answer correctness in run 2 | 75% | 95% |

Per question (correct / confidence):

| question | qwen2.5:3b | qwen3:4b-instruct |
|---|---|---|
| R01 | ok / Medium | ok / Medium |
| R02 | WRONG / Medium | ok / High |
| R03 | WRONG / Medium | ok / Medium |
| R04 | ok / Medium | ok / Medium |
| R05 | WRONG / High | WRONG / High |
| R06 | ok / Medium | ok / Medium |
| R07 | ok / Medium | ok / Medium |
| R08 | WRONG / Medium | ok / High |
| R09 | ok / Medium | ok / Medium |
| R10 | ok / Low | ok / Medium |
| R11 | ok / Medium | ok / Low |
| R12 | ok / High | ok / High |
| S01 | ok / Medium | ok / Medium |
| S02 | ok / High | ok / High |
| S03 | ok / High | ok / High |
| S04 | ok / Medium | ok / Medium |
| S05 | ok / Medium | ok / Medium |
| S06 | WRONG / Low | ok / Low |
| S07 | ok / High | ok / Medium |
| S08 | ok / Medium | ok / Medium |
| U01 | ok / Low | ok / Low |
| U02 | ok / Low | ok / Low |
| U03 | ok / Low | ok / Low |
| U04 | ok / Low | ok / Low |
| U05 | ok / Low | ok / Low |

## D. Cited summaries (5 component reports, 2 document summaries)

| metric | qwen2.5:3b | qwen3:4b-instruct |
|---|---|---|
| component reports: claim sentences cited (final) | 100% | 100% |
| component reports: cited before the filter | 100% | 100% |
| document summaries: claim sentences cited (final) | 100% | 100% |
| document summaries: cited before the filter | 79% | 98% |
| component reports produced | 5 | 5 |
| document summaries produced | 2 | 2 |

## E. Controls (model independent; run once with qwen2.5:3b)

| control | result | detail |
|---|---|---|
| unapproved_upload_rejected | PASS | IngestRejected: source_status 'unapproved' not allowed (approved/synthetic only) |
| cross_project_query_empty | PASS | non-member ask denied=True; chassis search for HMI-only terms returned 8 hits, 0 from other projects |
| viewer_cannot_approve | PASS | PermissionDenied: 'viewer' has role 'viewer' in 'synthetic-ecu', needs 'reviewer' |
| offline_run_works | PASS | {'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'ask_ok': True, 'ask_mode': 'generated', 'extract_ok': True, 'extract_entities': 10} |

## F. Speed and memory (latency from run 2 = warm; cold start before run 1)

| metric | qwen2.5:3b | qwen3:4b-instruct |
|---|---|---|
| Q&A end-to-end p50 / p95 | 2.8 s / 3.4 s | 5.1 s / 9.3 s |
| Q&A generation call p50 / p95 | 2.7 s / 3.3 s (n=22) | 5.2 s / 9.3 s (n=22) |
| extraction per section p50 / p95 | 4.3 s / 14.6 s (n=44) | 10.6 s / 72.5 s (n=43) |
| cold start (load + first token) | 6.7 s (load 4.5 s) | 11.2 s (load 8.8 s) |
| `ollama ps` placement | 100% GPU, 2059 MiB | 33%/67% CPU/GPU, 3362 MiB |
| peak GPU memory used (nvidia-smi, every 2 s) | 3503.0 of 4096.0 MiB (idle 1139.0 MiB) | 3331.0 of 4096.0 MiB (idle 1139.0 MiB) |
| fits fully on the 4 GB GPU | yes | no |
| wall time of one full run (run 2) | 11 min | 32 min |

GPU memory includes other processes on the GPU (idle value above); only one model is resident at a time.

qwen3 tag: qwen3:4b-instruct lists the 'thinking' capability in Ollama; every call sends think=false and <think>...</think> blocks are stripped before JSON/citation parsing (none were observed in a manual check).

Files: eval/results.json (all numbers), eval/charts/*.png, eval/run.log.

## Deployed configuration (author's post-hoc choice)

The pre-set decision rule above favoured **qwen2.5:3b** as the single default (failed: fits_gpu: qwen3:4b-instruct runs only partly on the 4 GB GPU). After seeing the results the author chose a split instead, and this choice was made post hoc, not by the rule:
- Q&A, component reports and document summaries: **qwen3:4b-instruct** (DEMO_MODEL), warmed at start-up and kept loaded for 30m (DEMO_KEEP_ALIVE): +20 points gold Q&A correctness in block C.
- LLM prose extraction: **qwen2.5:3b** (LLM_EXTRACT_MODEL): equal or better extraction F1, fewer defect false positives and 5x faster per section in blocks A, B and F.

### End-to-end check of this setup (2026-10-07 23:41; real API server, LLM cache reads off)

| step | result |
|---|---|
| server start + warm-up (embedder + 1-token Q&A model prompt) | 43.7 s; loaded: qwen3:4b-instruct (67% GPU) |
| 25 gold questions: correctness / refusal accuracy | 95% / 100% (models used: qwen3:4b-instruct) |
| 25 gold questions: end-to-end latency p50 / p95 (all / generated answers) | 5.4 s / 10.4 s; 6.8 s / 10.4 s |
| upload + ingest + LLM extraction + checks (synthetic HLD, 'deployed_check_hld') | HTTP 200, 32.6 s; extracted {'components': 8, 'ports': 17, 'interfaces': 8, 'signals': 6} |
| extraction calls | 3 by qwen2.5:3b; first call 15.0 s (includes loading the extraction model), others p50 6.8 s |
| loaded after the extraction | qwen2.5:3b (100% GPU) |
| ask right after the extraction (qwen3:4b-instruct) | 13.4 s |
| same ask again (warm) | 3.8 s |
| model-swap delay (first ask after extraction - warm ask) | 9.6 s |
| loaded at the end | qwen3:4b-instruct (67% GPU) |

## Final configuration (author's decision after seeing the comparison)

- Q&A answers, component reports and document summaries: **qwen3:4b-instruct** (DEMO_MODEL): 95% vs 75% gold Q&A correctness in block C.
- LLM prose extraction and everything run at upload time: **qwen2.5:3b** (LLM_EXTRACT_MODEL): faster, fewer false defects, fits fully in the 4 GB GPU; extraction runs once per upload, not while someone waits for an answer.
- This decision was taken after seeing the numbers. The pre-set rule above favoured qwen2.5:3b for everything because qwen3:4b-instruct does not fit >= 90% on the GPU.
- Routing by purpose (qa/summary/report -> DEMO_MODEL; extract/upload -> LLM_EXTRACT_MODEL); overrides only from ALLOWED_MODELS; think=false on every call; the Q&A model has keep_alive 30m, is the only model warmed at start-up and is re-warmed after upload extraction; model loads are traced as 'model_swap' in data/traces.jsonl.

### End-to-end run (2026-10-08 00:04; real API, LLM cache reads off, copies of the demo data)

| step | result | time | ok |
|---|---|---|---|
| a. cold start (models unloaded, uvicorn start -> /health ready after warming the Q&A model) | warmup ready steps {'embedder': 'ok', 'llm_load_ms': 8326, 'llm': 'ok'}; loaded: ['qwen3:4b-instruct 67% GPU / 33% CPU'] | 51.7 s | PASS |
| b. upload approved synthetic v1 (LLM extraction + checks + Q&A re-warm) | HTTP 200; ingest 5.4 s, extraction (qwen2.5:3b) 28.1 s, checks 18 ms, re-warm 12.7 s (load 10.3 s); entities 38 ({'components': 7, 'ports': 17, 'interfaces': 8, 'signals': 6}), findings 8; loaded after: ['qwen3:4b-instruct 67% GPU / 33% CPU'] | 46.8 s | PASS |
| c. draft / non-PDF / duplicate uploads rejected with a reason (400, audit-logged) | draft: 400 'source_status 'draft' not allowed (approved/synthetic only)'; non-PDF: 400 'not a PDF file'; duplicate: 400 'duplicate sha256 in project' | 1.9 s | PASS |
| d. six questions as viewer (3 answerable, 1 release, 1 off-topic refused, 1 not found) | 6/6 as expected; latency p50 4.7 s; models ['-', 'qwen3:4b-instruct']; loaded after Q&A: ['qwen3:4b-instruct 67% GPU / 33% CPU'] | 27.2 s | PASS |
| e. viewer approve/upload/audit forbidden, reviewer approves a finding | viewer approve 403, viewer upload 403, viewer audit log 403, reviewer approve 200 | 2.5 s | PASS |
| f. compare v1->v2 and export inventory/findings/diff as xlsx/csv/json (open + review_status) | diff entity changes 9 ({'components': {'added': 1, 'removed': 0, 'changed': 1, 'possible_renames': 0}, 'ports': {'added': 1, 'removed': 1, 'cha); inventory.xlsx ok, inventory.csv ok, inventory.json ok, findings.xlsx ok, findings.csv ok, findings.json ok, diff.xlsx ok, diff.csv ok, diff.json ok | 8.3 s | PASS |
| g. ask in chassis about content that exists only in synthetic-ecu/e2e-scratch (no leak) | refused not_found; citations -; leaked: none | 1.6 s | PASS |
| h. offline (HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1): restart, one ask, one LLM re-extraction | ready in 40.2s; ask generated by qwen3:4b-instruct in 5.7s; re-extract HTTP 200 {'components': 7, 'ports': 17, 'interfaces': 8, 'signals': 6} extraction 28.7 s, re-warm 11.8 s | 48.9 s | PASS |
| b'. chunks of the uploaded document indexed in the scratch project's collection | 16 chunks | - | PASS |
| i. peak VRAM during the whole run (nvidia-smi every 2 s) | peak 3493.0 of 4096.0 MiB; swaps none -> qwen3:4b-instruct 8.3s; qwen3:4b-instruct -> qwen2.5:3b 6.5s; qwen2.5:3b -> qwen3:4b-instruct 10.3s; qwen3:4b-instruct -> qwen2.5:3b 6.7s; qwen2.5:3b -> qwen3:4b-instruct 9.3s | - | PASS |

Questions (step d):

| question | mode | confidence | model | latency | citations | answer |
|---|---|---|---|---|---|---|
| ports of a named component: Which ports does the WinCtrlManager have? | generated | High | qwen3:4b-instruct | 5.3 s | e2e_hld_v1 p.4 | According to the extracted inventory of e2e_hld_v1, the ports of WinCtrlManager are: RpSwitchCmd (required, interface ISwitchCmd, p.6, table); RpPosition (requi |
| a section: What happens in the manual window operation flow? | generated | Medium | qwen3:4b-instruct | 8.1 s | e2e_hld_v1 p.8 | WinSwitchHandler reads the switch state and sends SwitchRequest to WinCtrlManager [C1]. WinCtrlManager validates the request against the current window state [C |
| an identifier: Who sends the MotorCommand signal and who receives it? | generated | High | qwen3:4b-instruct | 4.4 s | e2e_hld_v1 p.7 | According to the extracted inventory of e2e_hld_v1, signal MotorCommand is sent by WinCtrlManager and received by MotorDriver (p.7, table) [C1]. |
| a specific release: In release 1.0 of the Window Lift ECU HLD, which interfaces does the AntiPinchMonitor require? | generated | High | qwen3:4b-instruct | 4.9 s | syn_hld_v1 p.4 | According to the extracted inventory of syn_hld_v1, the required interfaces of AntiPinchMonitor are: IWinPosition (port RpPosition, required, p.6, table); IVehS |
| off-topic: What is a good recipe for pancakes with blueberries? | refused off_topic | Low | - | 674 ms | - | I cannot answer this from the project documents. |
| on-topic but missing: What ASIL level is assigned to the WinCtrlManager? | refused not_found | Low | - | 3.8 s | - | I cannot answer this from the project documents. |

Model loads / swaps (data/traces.jsonl, Ollama load_duration):

| from | to | load time |
|---|---|---|
| none | qwen3:4b-instruct | 8.3 s |
| qwen3:4b-instruct | qwen2.5:3b | 6.5 s |
| qwen2.5:3b | qwen3:4b-instruct | 10.3 s |
| qwen3:4b-instruct | qwen2.5:3b | 6.7 s |
| qwen2.5:3b | qwen3:4b-instruct | 9.3 s |

Placement: after Q&A qwen3:4b-instruct 67% GPU / 33% CPU; after the upload (incl. re-warm) qwen3:4b-instruct 67% GPU / 33% CPU; after the offline re-extraction (incl. re-warm) qwen3:4b-instruct 67% GPU / 33% CPU. Peak GPU memory 3493.0 of 4096.0 MiB (includes ~1.1 GB used by other processes).

### Gold questions with the final routing (25, qwen3:4b-instruct)

| metric | block C (qwen3, eval harness) | final configuration (API) |
|---|---|---|
| answer correctness | 95% | 95% |
| refusal accuracy | 100% | 100% |
| end-to-end latency p50 / p95 | 5.1 s / 9.3 s | 5.7 s / 10.6 s |
| wrong | R05 | R05 |

## Acceptance run (2026-10-08 14:36)

| # | component | result | evidence |
|---|---|---|---|
| 1 | Environment | PASS | ollama list has ['qwen2.5:3b', 'qwen3:4b-instruct'] (missing none); /health ok, warm-up ready; embedder bge (BAAI/bge-small-en-v1.5) fallback=False; SQLite integrity ok; chroma 17 files |
| 2 | Ingestion | PASS | real upload HTTP 200, synthetic HTTP 200; acc_chassis_small: 18 pages, 32 sections, 3 tables, metadata ['Document Classification', 'Document Identification No', 'Document Owner', 'Document Responsibility'], 3 change-history rows; acc_syn_v1: 9 pages, 18 sections, 5 tables, metadata [], 1 change-history rows; chunks {'acc_chassis_small': 37, 'acc_syn_v1': 16}; rejected: draft 400 'source_status 'draft' not allowed (approved/synthetic only)'; non-PDF 400 'not a PDF file'; duplicate 400 'duplicate sha256 in project'; oversized 400 'file too large (51.0 MB > 50 MB, MAX_UPLOAD_MB)'; path traversal  |
| 3 | OCR | PASS | HTTP 200; OCR pages [(1, 'ok', 58.9), (2, 'ok', 92.5)]; 1145 chars of text; 4/4 chunks flagged ocr; question generated citing acc_syn_v1 p.5, acc_syn_v1 p.6, acc_scanned p.2, acc_syn_v1 p.8; OCR note in answer: True ('bers with their cited chunk; check them. OCR text may contain recognition errors (C3 from OCR, confidence 91).') |
| 4 | Extraction | PASS | bodycomfort_r3.2.3 {'components': 49, 'ports': 143, 'interfaces': 7, 'signals': 29} by {'heuristic': 23, 'diagram': 205}; chassis_r3.2.3 {'components': 10, 'ports': 0, 'interfaces': 0, 'signals': 0} by {'heuristic': 10}; hmi_r25-11 {'components': 5, 'ports': 0, 'interfaces': 5, 'signals': 0} by {'heuristic': 5, 'table': 5}; syn_hld_v1 {'components': 7, 'ports': 17, 'interfaces': 8, 'signals': 6} by {'table': 38}; junk names: none; labels truncated in the source figure, kept as printed (lookups use the instance part): ['bodycomfort_r3.2.3:RainSensor::Rain...']; WiperWasher composition children  |
| 5 | Checks | PASS | synthetic v1 (scratch upload) findings per rule {'M1': 1, 'M2': 1, 'M3': 1, 'M4': 1, 'C1': 1, 'C2': 1, 'C3': 1, 'C4': 1}; planted defects found 8/8, missed none; real BodyComfort findings 9, labelled 'potential, needs review': 9/9 |
| 6 | Compare | PASS | syn_hld_v1->syn_hld_v2: entities {'components': {'added': 1, 'removed': 0, 'changed': 1}, 'ports': {'added': 1, 'removed': 1, 'changed': 3}, 'interfaces': {'added': 0, 'removed': 0, 'changed': 1}, 'signals': {'added': 0, 'removed': 0, 'changed': 1}}; sections {'added': 0, 'removed': 0, 'modified': 5, 'front_matter': 2}; diff export xlsx ok, csv ok, json ok / bodycomfort_r3.1.5->bodycomfort_r3.2.3: entities {'components': {'added': 1, 'removed': 0, 'changed': 1}, 'ports': {'added': 12, 'removed': 0, 'changed': 1}, 'interfaces': {'added': 0, 'removed': 0, 'changed': 0}, 'signals': {'added': 0, ' |
| 7 | Graph | PASS | BodyComfort R3.2.3: 228 nodes, 224 edges, 49 components; external scripts/styles in the HTML: 0; focus InteriorLightManager depth 1: 44 nodes, 43 edges |
| 8 | Reports | PASS | InteriorLightManager: 3 purpose sentences, uncited 0, model qwen3:4b-instruct, confidence Medium, limitation 'Some uncited sentences were removed.'; MirrorAdjust Manager: 4 purpose sentences, uncited 0, model qwen3:4b-instruct, confidence High, limitation '-'; WinCtrlManager: 4 purpose sentences, uncited 0, model qwen3:4b-instruct, confidence High, limitation '-'; flow summary bodycomfort_r3.2.3: 10 flows, 35 steps, uncited 0; flow summary syn_hld_v1: 3 flows, 13 steps, uncited 0 (uncited sentences are removed before display: the limitation says so when it happens) |
| 9 | Q&A (API + UI) | FAIL | API 11/12 as expected; latency p50 3.9 s p95 16.9 s (LLM cache emptied first: all first asks are real calls); repeated question 141 ms vs first ask 9.8 s (first cached False, repeat cached True, same answer True); UI 12/12 rendered the same outcome as the API, page errors 0 |
| 10 | Roles and controls | PASS | viewer approve 403, viewer upload (POST /projects/{pid}/upload) 403, viewer audit 403, reviewer approve 200, admin upload 200, non-member documents 403, non-member ask 403, non-member upload 403; audit log actions ingest 12, ingest_rejected 6, upload_pipeline 4, review 2, ask 101, re_extract 1, audit_denied 2 |
| 11 | Exports | PASS | inventory.xlsx ok; inventory.csv ok; inventory.json ok; findings.xlsx ok; findings.csv ok; findings.json ok; diff.xlsx ok; diff.csv ok; diff.json ok; report14.xlsx ok; report14.csv ok; report14.json ok; report15.xlsx ok; report15.csv ok; report15.json ok (each file opened: review_status column and citation/page/source column present) |
| 12 | UI | PASS | 4 pages x 2 widths load (/@1280: h-scroll 0px; /architecture@1280: h-scroll 0px; /review@1280: h-scroll 0px; /documents@1280: h-scroll 0px; /@800: h-scroll 0px; /architecture@800: h-scroll 0px; /review@800: h-scroll 0px; /documents@800: h-scroll 0px); page errors 0, console errors 0 []; Ports tab selects: True; Esc closes the side panel: True; Settings switches project: True; samples answerable 4/4 (Which ports does the WinCtrlManager have? / What does section 2.1 Component Overview describe? / Who sends SwitchRequest and who receives it? / What will the weather be like tomorrow?); light theme |
| 13 | Models | PASS | Q&A answers by ['qwen3:4b-instruct']; upload extraction by qwen2.5:3b (calls in trace: 3, models ['qwen2.5:3b']); think=false in the request body of both models: True; model swaps in data/traces.jsonl: ['none -> qwen3:4b-instruct 9.1s', 'qwen3:4b-instruct -> qwen2.5:3b 6.7s', 'qwen2.5:3b -> qwen3:4b-instruct 9.6s']; `ollama ps`: ['qwen3:4b-instruct 67% GPU / 33% CPU']; peak VRAM so far 3655.0 MiB |
| 14 | Offline | PASS | HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1: ready in 40.2s, embedder fallback False; ask generated (inventory_list); LLM re-extraction HTTP 200 {'components': 7, 'ports': 17, 'interfaces': 8, 'signals': 6} by qwen2.5:3b in 28.7 s |
| 15 | Cold start and demo reset | PASS | cold start (models unloaded -> /health ready, Q&A model warmed): 49.4 s (warm-up steps {'embedder': 'ok', 'llm_load_ms': 7816, 'llm': 'ok'}); scripts/demo_reset.py from a clean state (new data dir, no LLM cache): exit 0, total 486 s; last lines: Loading weights:   0%/          / 0/199 [00:00<?, ?it/s] / Loading weights: 100%/â–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆ/ 199/199 [00:00<00:00, 2199.90it/s] / Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN to enable higher rate limits and faster downloads. / Loading weights:   0%/          / 0/199 [00:00<?, ?it/s] / Loadi |
| 16 | Tests | PASS | pytest -q: 446 passed, 8 deselected, 2 warnings in 103.81s (0:01:43); pytest -q -m llm: 8 passed, 446 deselected, 1 warning in 116.71s (0:01:56) |
| 17 | Gold regression (run once, not tuned) | FAIL | 25 gold questions (qwen3:4b-instruct, cache reads off): correct 95%, citation accuracy 95%, section accuracy 85%, refusal 100% (last recorded 95% / 95% / 85% / 100%); 51 real-gold rows, mean page-scoped F1: deterministic 0.733 vs 0.721 recorded {'component': 0.595, 'port': 0.937, 'interface': 0.667}; qwen2.5:3b 0.756 vs 0.757 recorded {'component': 0.664, 'port': 0.937, 'interface': 0.667} |
