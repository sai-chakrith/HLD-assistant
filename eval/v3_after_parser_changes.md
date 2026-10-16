# v3 before -> after the parser generalisation changes (Task 3b/3c)

**v3 is no longer held-out**: its generator and PDF were read to diagnose the failures, so the 'after' numbers are in-sample for v3 and only show that the diagnosed failure classes are fixed (general mechanisms: fuzzy header aliases, column-role inference, prose port rules, TOC-less heading detection, LLM port/signal schema). The genuinely unseen test is the new held-out document, not v3. 'Before' = eval/results.md.

## v3 extraction (before -> after)

| entity type | deterministic | qwen2.5:3b |
|---|---|---|
| components | P n/a / R 0.00 / F1 0.00  ->  **P 1.00 / R 1.00 / F1 1.00** | P 1.00 / R 0.43 / F1 0.60  ->  **P 1.00 / R 1.00 / F1 1.00** |
| ports | P n/a / R 0.00 / F1 0.00  ->  **P 1.00 / R 1.00 / F1 1.00** | P 0.00 / R 0.00 / F1 0.00  ->  **P 1.00 / R 1.00 / F1 1.00** |
| interfaces | P n/a / R 0.00 / F1 0.00  ->  **P 1.00 / R 1.00 / F1 1.00** | P 1.00 / R 0.33 / F1 0.50  ->  **P 1.00 / R 1.00 / F1 1.00** |
| signals | P n/a / R 0.00 / F1 0.00  ->  **P 1.00 / R 1.00 / F1 1.00** | P n/a / R 0.00 / F1 0.00  ->  **P 1.00 / R 1.00 / F1 1.00** |
| micro_all | P n/a / R 0.00 / F1 0.00  ->  **P 1.00 / R 1.00 / F1 1.00** | P 0.62 / R 0.16 / F1 0.25  ->  **P 1.00 / R 1.00 / F1 1.00** |

## v1 extraction (after; before was 1.00 everywhere)

| entity type | deterministic | qwen2.5:3b |
|---|---|---|
| components | P 1.00 / R 1.00 / F1 1.00 | P 1.00 / R 1.00 / F1 1.00 |
| ports | P 1.00 / R 1.00 / F1 1.00 | P 1.00 / R 1.00 / F1 1.00 |
| interfaces | P 1.00 / R 1.00 / F1 1.00 | P 1.00 / R 1.00 / F1 1.00 |
| signals | P 1.00 / R 1.00 / F1 1.00 | P 1.00 / R 1.00 / F1 1.00 |
| micro_all | P 1.00 / R 1.00 / F1 1.00 | P 1.00 / R 1.00 / F1 1.00 |

## Planted defects and v1->v2 diff (after)

| check | deterministic | qwen2.5:3b |
|---|---|---|
| v1 defects found | 8/8 (false positives 0) | 8/8 (false positives 0) |
| v3 defects found | 6/6 (false positives 0) | 6/6 (false positives 0) |
| v1->v2 diff | 9/9 (fp 0) | 9/9 (fp 0) |

## Notes

- Root causes on v3 (before): ingest only recognised headings from page 5 on when no TOC page exists, and rejected header rows with unfamiliar words (P/R, Pattern, Producer, Consumer(s)), so tables fell into prose; empty table cells were collapsed, shifting later columns; prose ports had no deterministic rule.
- With the deterministic layer now saturating v3, the LLM layer adds nothing measurable on v3 (merge keeps table precedence); its value shows on prose-only content.
- Real documents (BodyComfort x2, Chassis x2, HMI): entity sets (names, per type) are byte-identical before and after the changes, both from the stored processed JSON and from re-parsing the PDFs with the new ingest.
