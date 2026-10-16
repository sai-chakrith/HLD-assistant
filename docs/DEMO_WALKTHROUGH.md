# Local demo walkthrough

Open the running demo at `http://127.0.0.1:8000/?project=synthetic-ecu`.
For a fresh launch, run `powershell -ExecutionPolicy Bypass -File .\start.ps1` from the project directory. Wait for the header to show **Ready** before presenting questions.

1. **Ask:** Begin with “What does MotorDriver do?” Point out the page citations, confidence, review status, and any limitation. Open a citation to jump to its source excerpt. The sample questions are checked against the current project before being offered.
2. **Architecture:** Open Architecture in the left navigation. Select v2.0, browse Components and Ports, filter by WinCtrlManager, and switch to Dependencies for the graph. Use Fit graph to reset the view. Open Review / edit to show the cited evidence and correction fields. Save a correction only when it reflects a source you have checked.
3. **Review:** Open Compare versions and compare v1.0 with v2.0. Discuss added, removed, and changed items. Findings are review candidates; confidence and counts do not establish extraction completeness. Open Export for the inventory or findings.
4. **Documents:** Browse the project library. Open Add document to reveal the upload form. It accepts approved or synthetic PDFs; processing a new document can take longer than the prepared walkthrough.
5. **Limits:** Ask “What will the weather be like tomorrow?” to demonstrate the documented scope. It should refuse instead of producing an unsupported answer.

Switch project from the project picker in the navigation to demonstrate separation between the synthetic ECU and the public reference documents. Navigation preserves the selected project. Use Settings to demonstrate seeded roles only when running in local demo mode.

The interface uses local fonts and assets. The current local-model setup supports questions without hosted model calls. Independent accuracy acceptance and user time-savings studies remain pending; this is a local demonstration, not production acceptance.

## Verification for the redesign

- 51 UI/API checks passed, including a real Chromium workflow, 375 px and 800 px layout checks, project-preserving navigation, upload rejection, answers, and persisted reviewer corrections.
- A live MotorDriver question returned a generated answer with source pages and a visible limitation notice.
- Raw UI check output: `eval/_work/ui/redesign_tests.log` and `eval/_work/ui/redesign_tests.xml`.
