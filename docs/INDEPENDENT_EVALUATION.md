# Independent evaluation protocol

The bundled gold questions, real_gold.csv, and synthetic v1/v2/v3 are development material. The existing v4 is synthetic and its limitations remain. None of these establishes independent performance on engineering HLDs.

## Before the next accuracy claim

1. An authorised document owner selects representative, unseen documents across layouts, projects, and releases. Keep proprietary files local. Separate development documents from the test set before implementation changes.
2. A reviewer who did not implement the parser records the expected components, port ownership/direction/interfaces, signal endpoints, and exact page evidence. Mark ambiguous items explicitly. Use a second reviewer to resolve disagreements; preserve both annotations and the resolution.
3. Hash the documents and annotation files, freeze the configuration and model versions, and record the evaluation date and reviewer identities supplied by those reviewers. Do not invent signatures or approvals. Changing labels or documents creates a new evaluation version.
4. Run once with no tuning on the frozen set. Report denominators, unmatched predictions, missing facts, incorrect fields, unsupported claims, correct refusals, false alarms, and latency. Score port ownership and direction jointly with the port name. Page-scoped precision on a partial reference list is not exhaustive precision.
5. Have engineers answer comparable tasks with and without the assistant, counterbalancing task order. Record elapsed time, correct answers, errors, corrections, and unresolved questions using docs/review_effort_protocol.md. Time saved has not yet been measured.
6. Choose acceptance thresholds with the intended reviewers before running the test. A high synthetic score or a successful demo is not an acceptance threshold.

## Current tools

`python -m scripts.audit_extraction --output eval/_work/extraction-audit.json` creates a development error audit with document and reference hashes, all deterministic predictions, missing facts, owner mismatches, and direction mismatches. It does not change the gold files or evaluate v4. `--root` supports auditing the preserved original code with the same PDFs.

The automated regression suite verifies mechanical behaviour. Live model checks verify specific examples. Neither substitutes for the independent annotation and timed study above. A genuinely independent evaluation remains pending until the document owner and reviewers provide those inputs.
