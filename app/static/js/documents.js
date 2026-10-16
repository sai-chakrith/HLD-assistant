// Documents screen: upload form (ingest + extract + checks run server-side) and the project's document table.
import { api, ctx, errorText, fmtDate, h, loadDocuments, openSettings, showState, verText } from "./api.js";

const tableBox = document.getElementById("docs-table");
const countEl = document.getElementById("docs-count");
const form = document.getElementById("upload-form");
const result = document.getElementById("upload-result");
async function renderDocs() {
  let docs;
  try {
    docs = await loadDocuments();
  } catch (e) {
    showState(tableBox, "error", errorText(e, "the document list"));
    return;
  }
  countEl.textContent = docs.length ? `(${docs.length})` : "";
  if (!docs.length) {
    tableBox.replaceChildren(h("p", { class: "state" },
      "This project has no documents yet; upload an approved or synthetic PDF using Add document."));
    return;
  }
  const rows = docs.map((d) => h("tr", {},
    h("td", { class: "wrap" }, h("a", { href: `/architecture?project=${encodeURIComponent(ctx.project)}&doc=${encodeURIComponent(d.doc_id)}` }, d.title || d.doc_id),
      h("span", { class: "sub" }, d.doc_id)),
    h("td", { class: "mono nowrap" }, d.version || ""),
    h("td", { class: "mono" }, d.doc_type),
    h("td", {}, d.source_status),
    h("td", { class: "num" }, d.pages ?? ""),
    h("td", { class: "nowrap" }, fmtDate(d.uploaded_at), h("span", { class: "sub" }, d.uploaded_by || "")),
    h("td", {},
      h("a", { href: `/review?project=${encodeURIComponent(ctx.project)}&doc=${encodeURIComponent(d.doc_id)}` }, "Review issues"))));
  tableBox.replaceChildren(h("div", { class: "table-wrap" }, h("table", { class: "docs-table" },
    h("thead", {}, h("tr", {}, ["Title", "Version", "Type", "Source status"].map((t) => h("th", { scope: "col" }, t)),
      h("th", { scope: "col", class: "num" }, "Pages"), h("th", { scope: "col" }, "Ingested"),
      h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Open")))),
    h("tbody", {}, rows))));
}

function timingText(t) {
  if (!t) return "";
  const sec = (x) => `${Math.round((x || 0) / 100) / 10} s`;
  return `Took ${sec(t.ingest_ms)} ingest, ${sec(t.extract_ms)} extraction, ${sec(t.checks_ms)} checks` +
    (t.rewarm_ms ? `, ${sec(t.rewarm_ms)} reloading the Q&A model. ` : ". ");
}

function summary(out) {
  const ex = out.extracted || {};
  const parts = Object.entries(ex).map(([k, v]) => `${v} ${k}`);
  return h("div", { class: "note ok" },
    h("strong", {}, `Ingested ${out.doc_id} (${verText(out.version)}). `),
    `Extracted ${parts.join(", ") || "no entities"}; ${out.findings} finding${out.findings === 1 ? "" : "s"} from the checks. `,
    timingText(out.timings),
    h("a", { href: `/architecture?doc=${encodeURIComponent(out.doc_id)}` }, "Open in Architecture"), " · ",
    h("a", { href: `/review?doc=${encodeURIComponent(out.doc_id)}` }, "Open in Review"));
}

form?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(form);
  const file = fd.get("file");
  if (!file || !file.name) {
    result.replaceChildren(h("div", { class: "note error", role: "alert" }, "Choose a PDF file first."));
    return;
  }
  const btn = document.getElementById("upload-btn");
  btn.disabled = true;
  const started = Date.now();
  const line = h("span", {}, "");
  const tick = () => {
    const s = Math.round((Date.now() - started) / 1000);
    line.textContent = `Processing ${file.name}: parsing, indexing, extracting entities (local LLM), running the ` +
      `checks, then reloading the Q&A model (about 10 s extra on a 4 GB GPU). ${s} s elapsed ` +
      `(a short document takes about 1 min, a long one several minutes).`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  result.replaceChildren(h("div", { class: "note" }, line));
  try {
    const out = await api(`/projects/${encodeURIComponent(ctx.project)}/upload`, { method: "POST", form: fd });
    result.replaceChildren(summary(out));
    form.reset();
    renderDocs();
  } catch (e) {
    const reason = e.status === 400 ? "Rejected" : e.status === 403 ? "Not allowed" : "Upload failed";
    const tail = e.status === 400 ? " The attempt is recorded in the audit log." : "";
    result.replaceChildren(h("div", { class: "note error", role: "alert" },
      h("strong", {}, `${reason}: `), e.message, tail));
  } finally {
    clearInterval(timer);
    btn.disabled = false;
  }
});

renderDocs();
