# Repair verification - 16 October 2026

Exact section-number questions now retrieve matching section metadata within project and version scope. Ask displays cited source excerpts, explicitly labelled as excerpts rather than model-written summaries. Unknown numbers do not match neighbouring sections. Confidence thresholds have not been lowered.

Prose extraction now sends at most 500 characters per request instead of 3,000, asks for short verbatim evidence, and bounds component descriptions to 120 characters. The real AUTOSAR passage test passed with five requests, each below the existing 180-second timeout. End-to-end extraction still took about 214 seconds on this machine; this is not a general speed or accuracy benchmark.

The model-default, rewarm, removed-banner and browser-history tests now reflect the current requirements. The browser fixture disables local .env loading, uses the existing project Chromium installation, and calibrates its hashing-only retrieval separately from production BGE scoring.

Verification:
- Full regression suite: 485 passed, 8 model tests deselected. Includes the real Chromium workflow.
- Authenticated live feature sweep: 58 passed out of 58.
- Previously failing real-passage Qwen3 extraction: 1 passed; source-grounding assertions retained.
- The other seven model checks passed in the preceding test run; they were not rerun after this targeted extraction repair.

Evidence: eval/final_repaired_regression.xml; eval/compact_extraction.xml; eval/repaired_20261016/live.json; eval/repaired_20261016/summary.json.

Remaining boundaries: long-form extraction and document-wide generation can still take minutes. These checks establish software and integration behaviour, not independent RAGAS scores, semantic accuracy, complete extraction coverage or production readiness.
