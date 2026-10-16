# Review-effort measurement protocol

Goal: measure how much author time the tool saves when reviewing an HLD, manual vs tool-assisted, on the same
tasks and checklist. Until this is run, the review-effort metric is reported as "not measured" in `eval/results.md`.

Subject: `data/synthetic/hld_v1.pdf` (Window Lift ECU HLD v1). Do NOT open `data/synthetic/ground_truth.json`,
`data/synthetic/generate_hld.py` or the tool's findings before Part A is finished: they contain the answers.

## Timer

All timing uses `python scripts/review_timer.py` (writes `eval/review_effort.json`).

```text
python scripts/review_timer.py start manual     # begin Part A (stop/start again for breaks)
python scripts/review_timer.py note "unsure about port table on p4" --phase manual
python scripts/review_timer.py stop manual
python scripts/review_timer.py found manual 6 --ids "defect-1,defect-2"   # AFTER timing, see Scoring
python scripts/review_timer.py status
```

Phase names are `manual` and `tool`. The clock only runs between `start` and `stop`; filling in the answer sheet
counts as review time (do it inside the timed window), scoring does not.

## Tasks and checklist (identical for A and B)

Write answers on a sheet, one line per item. Items are derived from the tool's rule families, phrased as questions.

1. List every component (name and one-line role).
2. List every port of every component with direction and interface.
3. List every interface with its kind, and every signal with its sender and receivers.
4. Required ports: does each required port with an interface have a matching provided port of the same interface on
   another component?
5. Kinds: for each interface, do the kinds stated in the interface table and in the port tables agree?
6. Signals: does every signal have both a sender and at least one receiver?
7. Names: are there near-duplicate component or interface names that look like the same thing spelled twice?
8. Flows: is every component mentioned in the functional-flow section(s)?
9. Ports: does every port name an interface?
10. Interfaces: is every interface used by a port also defined in an interface table?
11. Components: does every component have a description?

Report each inconsistency you believe is a defect as: checklist item number, entities involved, page.

## Part A: manual (PDF only)

Open only the PDF. No tool, no scripts, no search over extracted data (PDF reader find is allowed).
Start the `manual` timer, complete tasks 1 to 11, stop the timer. Do not look at any answer yet.

## Part B: with the tool

Ingest and extract the same document through the UI/API (or use the existing `synthetic-ecu` project), then complete
the same tasks 1 to 11 using the inventory, graph, checks and report views. The tool's own findings count as work
product only if you review them: confirm or reject each one against the PDF and record the decision. Start the `tool`
timer when you open the tool on a prepared project (ingest time included if you ingest yourself; state which in a
note) and stop it when the answer sheet is complete.

## Scoring (after both timers are stopped)

Only now open `data/synthetic/ground_truth.json` (`planted_defects`, version 1.0). For each phase count the planted
defects your sheet matches (same rule family and same entities), and the false reports (claims matching no planted
defect). Record with `found <phase> <n> --ids ...`. `eval/run_eval.py` reads these numbers.

Note any defect the tool found that you missed manually and vice versa; this tells whether time was saved by being
faster or by finding less.

## Learning effect and mitigation

Doing Part A first gives the author knowledge of the document that makes Part B faster, so the reduction is
overstated; doing B first understates it. Mitigations, best first:

1. Counterbalance with a second person: person 1 does A then B, person 2 does B then A; average the reductions.
2. Use two different documents: A on `hld_v2.pdf` and B on `hld_v1.pdf` (or the reverse), keeping the checklist the
   same. v1 and v2 share most entities, so allow at least one day between the parts and report that v2 has fewer
   planted defects (`remaining_defects_v2`) when comparing found counts.
3. If neither is possible, run A first, wait a few days, then B, and state the order as a limitation.

Also state: n is tiny (one or two reviewers, one synthetic document), the author knows the tool, and synthetic
documents are cleaner than real ones, so treat the result as an indicative case study, not a general claim.

## Results table (fill in)

| Run | Reviewer | Document | Order | Manual min | Tool min | Reduction % | Defects found manual | Defects found tool | False reports manual | False reports tool |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 |  | hld_v1.pdf |  |  |  |  |  /8 |  /8 |  |  |
| 2 |  |  |  |  |  |  |  |  |  |  |

Reduction % = (manual - tool) / manual x 100. Planted defects in v1: 8 (see ground truth). After filling in,
`python eval/run_eval.py --render-only` (full or `--quick`) adds a "Review effort" section to `eval/results.md`.
