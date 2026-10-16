# Demo check (docs/outcomes.md section 3)

Run on 2026-10-07 against a live `uvicorn app.api:app` on the real DB (real bge embedder, HF offline flags, Ollama
Ollama: a local model, the then-default one, since removed), driven by `scripts/demo_check.py` (HTTP calls exactly as the UI makes them). Raw evidence:
`data/demo_check.json`. Uploads and review actions went to a scratch project `demo-check` that the script creates and
removes, so real projects keep their data. The UI itself was exercised with Streamlit AppTest against the live API
(`scripts/ui_walk.py`) and `streamlit run` was started headless (health `ok`, page HTTP 200).

| # | Scenario | Result | Evidence |
|---|---|---|---|
| 1 | Upload without approved/synthetic status is rejected and logged | PASS | HTTP 400 "source_status 'draft' not allowed"; no document stored; audit row `ingest_rejected` |
| 2 | Approved BodyComfort upload, cited answer about a SWC's ports | PASS | Upload+extract+checks 26 s (49 components, 131 ports, 29 signals). "Which ports does FrontWasher have?" gave a generated answer, confidence Medium, model: the then-default local model, citations p.9 s4.1 and p.11 s4.1.1. Whether the port names in the answer are exactly right was not hand-verified |
| 3 | Question not in the document is refused with a limitation note | PASS | "maximum torque of the diesel engine": mode refused, Low, limitation "Best retrieval score 0.54 is below the threshold 0.65" |
| 4 | Synthetic v1: inventory and dependency graph | PASS | 7 components, 17 ports, 8 interfaces, 6 signals; graph 39 nodes / 53 edges; component filter (AntiPinchMonitor, depth 1) 8 nodes / 7 edges; evidence page rendered via the new PNG endpoint |
| 5 | Component report and functional-flow summary, every line cited | PASS | WinCtrlManager: 20 fact rows all cited, purpose has 3 citations; flow: 13 steps, 0 uncited. Purpose text is LLM-generated, only citation presence was checked automatically |
| 6 | Checks find planted defects with page evidence | PASS | 8/8 planted defects found, 8 findings total, 0 false positives, all with page evidence (synthetic v1) |
| 7 | Viewer cannot approve; reviewer approves and rejects; audit log | PASS | viewer review 403, `/audit` 403 for viewer; reviewer approve + reject 200; statuses approved/rejected; audit lists `review` actions |
| 8 | Compare v1 vs v2 | PASS | components +1/-0/~1, ports +1/-1/~3 match ground truth; total added 2, removed 1, changed 6; change history 1 vs 2 rows; XLSX export 8 KB |
| 9 | Switch project: other project's docs not retrievable | PASS | same FrontWasher question in `chassis` is refused with no citations; BodyComfort/synthetic doc pages return 404 through chassis; non-member 403 |
| 10 | Offline | PASS (with caveat) | A subprocess with every non-loopback socket connection refused and HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE set answered the same question with 2 citations. The network adapter was not physically disconnected |
| 11 | Export findings to Excel | PASS | XLSX with `review_status`, `doc_id`, `version`, `page`, `section` columns; statuses approved/rejected/unreviewed; "Contains unreviewed AI output" banner row |

## Fixed while walking through
- Q&A answers did not say which model produced them: added `model` to the answer payload.
- Evidence pages could not be shown from the UI: new page PNG endpoint; uploads now keep a copy of the PDF under
  `processed/<project>/pdfs/` so uploaded documents can render too.
- Graph could not be filtered: `component` and `depth` params added.
- The Audit tab was shown by project role; now it uses the global admin role from the new `/me` endpoint.

## Not verified
- Visual rendering (layout, badge colours, pyvis interactivity, image sizing). AppTest checks the element tree and
  absence of exceptions, not pixels. Nobody looked at the page in a browser.
- Streamlit file upload widget (scenarios 1, 2, 4 were run through the API, not the widget).
- `use_llm=true` upload (slow LLM extraction) from the UI checkbox.
- Docker (`docker compose up`) is Phase H.
