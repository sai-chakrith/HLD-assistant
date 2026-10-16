# Outcomes — AUTOSAR HLD Document Analysis Assistant

What the project delivers and how success is measured. Metrics mirror the case study's *Indicative Success Metrics*.

---

## 1. Deliverables

| # | Deliverable | Description |
|---|---|---|
| D1 | Working application | One FastAPI service (JSON API + browser UI), runnable via `uvicorn app.api:app` or `docker compose up` |
| D2 | Cited Q&A | Answers with page and section citations |
| D3 | Architecture inventory | Components, ports, interfaces, signals in SQLite + export |
| D4 | Dependency graph | Interactive graph view in the UI |
| D5 | Consistency report | Rule-based findings with source evidence and review status |
| D6 | Revision comparison | Entity-level diff between two document versions |
| D7 | Human review log | Approve / reject decisions stored with timestamps |
| D8 | Exports | CSV / Excel / JSON for inventory, findings, diff |
| D9 | Evaluation report | Metrics table for qwen2.5:3b (local) |
| D10 | Synthetic dataset | HLD v1/v2 PDFs + `ground_truth.json` + gold questions |
| D11 | Documentation | README, architecture diagram, setup guide, limitations |
| D12 | Demo video | 3–4 minutes, end-to-end walkthrough |
| D13 | Cited summaries and component reports | Document summary, per-component reports, and functional-flow summaries, every claim cited |
| D14 | Controls layer | Approved/synthetic upload gate, project isolation, roles, confidence badges, audit log |

### Requirement coverage

| Case study requirement | Deliverable |
|---|---|
| Document upload and controlled ingestion | D1, D14 |
| Section, table and metadata extraction | D1 |
| Architecture entity recognition | D3 |
| Interface and dependency mapping | D4 |
| Natural-language Q&A with citations | D2 |
| Cited summaries and component reports | D13 |
| Revision comparison | D6 |
| Potential mismatch and completeness checks | D5 |
| Output: component and interface inventory | D3 |
| Output: dependency maps | D4 |
| Output: functional-flow summaries | D13 |
| Output: searchable Q&A with citations | D2 |
| Output: potential inconsistency report | D5 |
| Output: structured export for downstream use | D8 |
| Control: approved / synthetic documents only, project isolation, access control | D14 |
| Control: answers restricted to evidence, citations, confidence / limitation indicators | D2, D14 |
| Control: human review before reliance | D7, D14 |

---

## 2. Success Metrics

| Case study metric | How it is measured here | Target |
|---|---|---|
| **Extraction quality** | Precision and recall of components, ports, interfaces, signals vs `ground_truth.json` | Precision ≥ 0.85, Recall ≥ 0.80 |
| **Answer groundedness** | % of answers whose citations point to a page/section that actually supports the answer (checked on the gold set) | ≥ 90% |
| **Issue discovery** | Planted defects found by consistency checks | ≥ 6 of 8 |
| **False positives** | Findings that are not real defects | ≤ 2 |
| **Revision comparison** | Correctly detected added / removed / changed entities between v1 and v2 | ≥ 90% |
| **Review effort** | Time to produce an inventory + issue list manually vs with the tool (on synthetic HLD) | Report measured reduction |
| **Summary groundedness** | % of claims in component reports and flow summaries backed by a valid citation | ≥ 90% |
| **Confidence calibration** | Answers marked *High* that are correct vs answers marked *Low* that are correct | High clearly above Low |
| **Control enforcement** | Unapproved uploads rejected; cross-project queries return nothing; viewer cannot approve | 100% of control tests pass |
| **Local feasibility** | End-to-end run on 4GB VRAM with 3B model; latency per query | Report p50 latency |

Results are reported for the single local model (qwen2.5:3b). Everything runs offline with no API keys, which is the case study's IP-protection requirement.

---

## 3. Demo Scenarios

1. Try uploading a file without approved/synthetic status → rejected and logged.
2. Upload the AUTOSAR Body & Comfort PDF as *approved* → ask about a specific SWC's ports → cited answer with confidence badge.
3. Ask something not in the document → refused with a limitation note.
4. Upload synthetic HLD v1 → view inventory and dependency graph.
5. Open a component report and a functional-flow summary → every line cited.
6. Run mismatch and completeness checks → show planted defects found, each with page evidence.
7. Log in as *viewer* → cannot approve. Switch to *reviewer* → approve one finding, reject one → show the audit log.
8. Compare v1 vs v2 → show added / removed / changed entities.
9. Switch project → previous project's documents are not retrievable.
10. Disconnect from the internet → same query still works, fully offline.
11. Export findings to Excel → rows show review status and citations.

---

## 4. Definition of Done

- [ ] `docker compose up` starts the full app from a clean clone
- [ ] All 11 demo scenarios work end to end
- [ ] Every row of the requirement coverage table is demonstrable
- [ ] Control tests pass (upload gate, project isolation, role checks)
- [ ] `eval/run_eval.py` prints the metrics table for both modes
- [ ] README contains architecture diagram, setup steps, metrics, limitations, and scale path
- [ ] No AUTOSAR PDFs committed; download script works
- [ ] Demo video recorded

---

## 5. Business Value (case study alignment)

| Case study benefit | How this pilot shows it |
|---|---|
| Reduced document-review effort | Measured time reduction on synthetic HLD |
| Faster architecture validation | Automated consistency checks with evidence |
| Improved knowledge reuse | Searchable, cited Q&A over architecture docs |
| Better onboarding | New engineers can query components and flows directly |
| Traceability to testing, safety, cybersecurity | Structured inventory exportable for downstream HARA / TARA / test use |
