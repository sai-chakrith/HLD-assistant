# Dev-set retrieval ablation

Generated 2026-10-07 21:12. Dev set: eval/dev_questions.json (30 questions: 8 ports, 6 version, 5 identifier, 5 unanswerable, 6 general), written from the PDFs/inventory. The gold set (eval/gold_questions.json, eval/real_gold.csv) was NOT used for any tuning. k=5, threshold 0.65, model qwen2.5:3b (LLM calls cached; latency is end-to-end wall time, cached generations included). hit@k: an expected doc+page among the first k retrieved chunks (inventory chunks count, section-expansion chunks do not). Refusal accuracy: unanswerable questions refused (retrieval columns: refused at the threshold before the LLM; e2e: final answer refused).

| configuration | hit@1 | hit@5 | hit@5 ports | hit@5 version | hit@5 identifier | refusal acc. (retrieval) | false refusals | e2e correct | key-fact recall | e2e refusal acc. | retrieval p50/p95 ms | e2e p50/p95 s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| dense only | 68% | 80% | 75% | 67% | 80% | 40% | 2 | 24% | 34% | 100% | 61/122 | 0.1/1.7 |
| +BM25 (RRF) | 80% | 92% | 88% | 83% | 100% | 40% | 0 | 36% | 46% | 100% | 65/147 | 0.1/0.2 |
| +query understanding / version policy | 88% | 96% | 88% | 100% | 100% | 40% | 0 | 52% | 59% | 100% | 78/181 | 0.1/0.2 |
| +inventory evidence | 88% | 100% | 100% | 100% | 100% | 40% | 0 | 68% | 72% | 100% | 78/309 | 0.1/0.2 |
| +section expansion | 88% | 100% | 100% | 100% | 100% | 40% | 0 | 56% | 67% | 100% | 110/233 | 0.1/0.3 |
| +reranker | 88% | 96% | 100% | 83% | 100% | 40% | 1 | 56% | 66% | 100% | 1567/2521 | 1.4/2.0 |

## Threshold sweep (full configuration, retrieval-level refusal)

| threshold | refusal accuracy | false refusals (of 25) |
|---|---|---|
| 0.55 | 20% | 0 |
| 0.56 | 20% | 0 |
| 0.57 | 20% | 0 |
| 0.58 | 20% | 0 |
| 0.59 | 20% | 0 |
| 0.60 | 20% | 0 |
| 0.61 | 40% | 0 |
| 0.62 | 40% | 0 |
| 0.63 | 40% | 0 |
| 0.64 | 40% | 0 |
| 0.65 | 40% | 0 |
| 0.66 | 40% | 0 |
| 0.67 | 40% | 1 |
| 0.68 | 60% | 1 |
| 0.69 | 80% | 4 |
| 0.70 | 80% | 4 |
| 0.71 | 100% | 4 |
| 0.72 | 100% | 4 |
| 0.73 | 100% | 4 |
| 0.74 | 100% | 4 |
| 0.75 | 100% | 4 |

## Part B changes (dev set, end-to-end, default retrieval, qwen2.5:3b)

Each row is the full dev set after that change (cumulative, in order). Calibration = share correct among answers with that confidence (n in brackets).

| change | e2e correct | key-fact recall | refusal acc. | false refusals | refused at retrieval (unanswerable / answerable) | High | Medium | Low | flipped vs previous row |
|---|---|---|---|---|---|---|---|---|---|
| 0 baseline (start of Part B) | 68% | 72% | 100% | 1 | n/a | 75% (4) | 70% (20) | 83% (6) | - |
| B1 structured inventory answers | 76% | 80% | 100% | 1 | n/a | 92% (12) | 67% (12) | 83% (6) | D03 +, D05 + |
| B2 change-history answers + release-difference note | 84% | 89% | 100% | 0 | n/a | 93% (14) | 73% (11) | 100% (5) | D13 +, D14 + |
| B3 evidence-support confidence | 84% | 89% | 100% | 0 | n/a | 100% (12) | 75% (12) | 83% (6) | - |
| B4 off-topic refusal at retrieval + not-found note | 84% | 89% | 100% | 0 | 40% / 0 | 100% (12) | 75% (12) | 83% (6) | - |
| B4b prompt: answer every part, use the evidence terms (REVERTED: net 0, High calibration 100% -> 92%) | 84% | 90% | 100% | 0 | 40% / 0 | 92% (13) | 90% (10) | 71% (7) | D15 +, D30 - |

## B5 extraction precision rules (non-gold pages, hand-checked; qwen2.5:3b cached, diagram + heuristics + llm)

Rules (app/extract.refine_entities, EXTRACT_REFINE=0 disables): drop 'Name: Type' annotations; drop compound component names (',' '/' '&' '(' and/or/incl); drop llm-only components whose verbatim quote adds < 3 words to the name; merge near-duplicate components (plural, acronym + expansion, spacing) under the best-ranked name (method rank, CamelCase, shorter). Designed on chassis, the DCM SWS and bodycomfort_r3.1.5 output; eval/real_gold.csv was not opened.

| document / pages | before | after |
|---|---|---|
| chassis_r3.2.3 pp. 8-15 components (truth: 10 SW-compositions, hand-checked) | P 0.91 R 1.00 F1 0.95 (FP: ABS, a sub-function of ESC) | P 0.91 R 1.00 F1 0.95 (unchanged) |
| chassis_r3.1.5 pp. 9-15 components (same 10) | P 0.91 R 1.00 F1 0.95 | P 0.91 R 1.00 F1 0.95 |
| DCM SWS interfaces pp. 555-754 (truth: 0; all are ECUC config parameters) | 12 extracted, 12 FP | 0 extracted |
| bodycomfort_r3.1.5 components (not scored) | 54 | 50: dropped 'Doors, KeylessAccess and RemoteKey', 'CentralLocking (including Doorlocks) / Remote Key / Keyless Access'; merged 'Exterior Lights' -> 'Exterior Light', 'Interior Light Sensors' -> 'Interior Light Sensor' |

Real-gold before/after is reported once, at the end (eval/results.md block A).

## Notes

- Section expansion (parent intro + next chunk) lowered end-to-end correctness on the dev set, so it is OFF by default (RETRIEVAL_EXPAND=false); the code stays switchable.
- Cross-encoder reranker (cross-encoder/ms-marco-MiniLM-L-6-v2, CPU) adds ~1.4 s per query (> 1 s budget) and did not improve end-to-end correctness, so it is OFF (RETRIEVAL_RERANK=false).
- The default configuration is the '+inventory evidence' row (hybrid + query understanding + inventory, no expansion, no reranker).
- SIMILARITY_THRESHOLD stays 0.65: the sweep shows no threshold that refuses more of the on-domain unanswerable questions without falsely refusing answerable ones; refusal of the unanswerable dev questions happens at the LLM/citation stage (100% end-to-end in every configuration). An exact identifier match lowers the bar by 0.05 (qa.IDENTIFIER_MARGIN).
- Latency: LLM calls are cached across runs, so end-to-end p50 can be low where prompts repeat between configurations; uncached generations take 3-5 s on the RTX 3050.
- Uncached end-to-end latency (first run of each configuration, p50/p95): dense only 3.2/3.9 s, +BM25 3.1/4.1 s, +query understanding 3.2/4.0 s, +section expansion 2.9/3.5 s, +reranker 4.7/5.9 s (+inventory reused cached prompts for most questions; its generations are the same size as +query understanding). The second run shown in the table hit the LLM cache.
