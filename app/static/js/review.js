// Review screen: issues grouped by rule (plain rule name, code after it) with approve / reject (only when the
// server sent the controls), version comparison (entity and section changes) and exports.
import {
  api, citeTag, ctx, download, errorText, fillDocPicker, h, loadDocuments, reviewLabel, setQuery, severityLabel, showState, verText,
} from "./api.js";

const P = `/projects/${encodeURIComponent(ctx.project)}`;
const RULE_ORDER = ["M1", "M2", "M3", "M4", "C1", "C2", "C3", "C4"];
const RULE_NAMES = JSON.parse(document.getElementById("rule-names").textContent);
const actionsTpl = document.getElementById("review-actions"); // absent for viewers

const docSelect = document.getElementById("doc-select");
const findingsBox = document.getElementById("findings");
const findingsNote = document.getElementById("findings-note");
const findingsCount = document.getElementById("findings-count");
const fmtSelect = document.getElementById("export-fmt");
const exportMsg = document.getElementById("export-msg");
const diffBtn = document.querySelector('[data-export="diff"]');
const docA = document.getElementById("doc-a");
const docB = document.getElementById("doc-b");
const compareBox = document.getElementById("compare");

let docs = [];
let current = "";
let lastPair = null; // [doc_a, doc_b] of the comparison on screen

// ------------------------------------------------------------------ findings

function reviewControls(f, statusSlot, msg) {
  if (!actionsTpl) return null;
  const node = actionsTpl.content.firstElementChild.cloneNode(true);
  node.querySelectorAll("[data-decision]").forEach((b) => b.addEventListener("click", async () => {
    const decision = b.dataset.decision;
    node.querySelectorAll("button").forEach((x) => { x.disabled = true; });
    msg.className = "finding-msg";
    msg.textContent = "Saving the decision.";
    try {
      await api(`${P}/review`, { method: "POST", json: { item_type: "finding", item_id: f.id, decision, comment: "" } });
      f.review_status = decision;
      statusSlot.replaceChildren(reviewLabel(decision, false));
      msg.textContent = `${decision === "approved" ? "Approved" : "Rejected"} by ${ctx.user}; recorded in the audit log.`;
    } catch (e) {
      msg.className = "finding-msg error";
      msg.textContent = `The decision was not saved: ${e.message}.`;
    } finally {
      node.querySelectorAll("button").forEach((x) => { x.disabled = false; });
    }
  }));
  return node;
}

function findingRow(f) {
  const statusSlot = h("span", {}, reviewLabel(f.review_status, false));
  const msg = h("span", { class: "finding-msg", "aria-live": "polite" });
  return h("div", { class: "finding" },
    h("div", {},
      h("div", { class: "finding-title" }, f.title),
      h("p", { class: "finding-expl" }, f.explanation),
      h("div", { class: "meta-row" },
        f.entities?.length ? h("span", { class: "tag" }, f.entities.join(", ")) : null,
        (f.evidence || []).map((e) => citeTag(e)))),
    h("div", { class: "finding-side" },
      h("div", { class: "meta-row" }, severityLabel(f.severity),
        f.label ? h("span", { class: "label medium", title: "Real (approved) document: verify before relying on it" }, f.label) : null,
        statusSlot),
      reviewControls(f, statusSlot, msg), msg));
}

async function loadFindings() {
  showState(findingsBox, "loading", "Loading the issues.");
  findingsCount.textContent = "";
  findingsNote.replaceChildren();
  let list;
  try {
    list = await api(`${P}/findings`, { params: { doc_id: current } });
  } catch (e) {
    showState(findingsBox, "error", errorText(e, "the issues"));
    return;
  }
  findingsCount.textContent = `(${list.length})`;
  const doc = docs.find((d) => d.doc_id === current);
  if (list.length && doc?.source_status === "approved") {
    findingsNote.replaceChildren(h("div", { class: "note warn" },
      "This is a real (approved) document and the rules were written for the synthetic HLD format, so each issue needs a person to confirm it."));
  }
  if (!list.length) {
    findingsBox.replaceChildren(h("p", { class: "state" },
      "No issues in this document: none of the eight checks matched."));
    return;
  }
  const byRule = new Map();
  for (const f of list) {
    if (!byRule.has(f.rule_id)) byRule.set(f.rule_id, []);
    byRule.get(f.rule_id).push(f);
  }
  const rank = (r) => (RULE_ORDER.includes(r) ? RULE_ORDER.indexOf(r) : RULE_ORDER.length);
  const rules = [...byRule.keys()].sort((a, b) => rank(a) - rank(b));
  findingsBox.replaceChildren(...rules.map((r) => {
    const items = byRule.get(r);
    return h("details", { class: "rule-group", open: items.length <= 10 },
      h("summary", {}, h("span", {}, RULE_NAMES[r] || "Other check"), h("span", { class: "code" }, r),
        h("span", { class: "muted" }, `${items.length} issue${items.length === 1 ? "" : "s"}`)),
      h("div", { class: "findings" }, items.map(findingRow)));
  }));
}

// ------------------------------------------------------------------ compare

const TYPES = ["components", "ports", "interfaces", "signals"];

function changeText(changes) {
  return Object.entries(changes || {}).map(([field, v]) => h("span", { class: "chg" },
    `${field}: `, h("span", { class: "old" }, fmtVal(v.old)), " → ", h("span", { class: "new" }, fmtVal(v.new)), "  "));
}

function fmtVal(v) {
  if (v === null || v === undefined || v === "") return "(empty)";
  return Array.isArray(v) ? v.join(", ") : String(v);
}

function entityTable(td) {
  const rows = [];
  for (const kind of ["added", "removed", "changed"]) {
    for (const e of td[kind] || []) {
      rows.push(h("tr", {},
        h("td", { class: "nowrap" }, h("span", { class: `kind ${kind === "changed" ? "modified" : kind}` }, kind)),
        h("td", { class: "mono" }, e.name),
        h("td", {}, kind === "changed" ? changeText(e.changes) : ""),
        h("td", { class: "nowrap" }, (e.citations || []).map((c) => [citeTag({ ...c }), " "]))));
    }
  }
  for (const r of td.possible_renames || []) {
    rows.push(h("tr", {}, h("td", {}, h("span", { class: "kind modified" }, "rename?")),
      h("td", { class: "mono" }, `${r.old} → ${r.new}`),
      h("td", { class: "tag", title: `similarity ${r.score}` }, `names ${Math.round(Number(r.score) > 1 ? Number(r.score) : Number(r.score) * 100)}% alike`), h("td", {})));
  }
  if (!rows.length) return null;
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Change"), h("th", { scope: "col" }, "Entity"),
      h("th", { scope: "col" }, "Details"), h("th", { scope: "col" }, "Source"))),
    h("tbody", {}, rows)));
}

function sectionChange(ch, bucket) {
  const pages = (p) => (p || []).filter(Boolean).join("-");
  const where = `A p.${pages(ch.pages_a) || "-"} / B p.${pages(ch.pages_b) || "-"}`;
  const sec = ch.section_b || ch.section_a || "";
  const kind = bucket === "front_matter" ? "front" : ch.change;
  const body = h("div", { class: "body" },
    (ch.sentences || []).map((s) => h("div", {},
      s.old ? h("div", { class: "old-text" }, h("span", { class: "kind removed" }, "old"), s.old) : null,
      s.new ? h("div", {}, h("span", { class: "kind added" }, "new"), s.new) : null)),
    (ch.rows_added || []).map((r) => h("div", { class: "mono" }, h("span", { class: "kind added" }, "+ row"), r)),
    (ch.rows_removed || []).map((r) => h("div", { class: "mono" }, h("span", { class: "kind removed" }, "- row"), r)));
  const hasBody = body.childNodes.length > 0;
  return h("details", { class: "section-change" },
    h("summary", {}, h("span", { class: `kind ${ch.change}` }, kind), " ",
      h("span", { class: "mono" }, sec), ` ${ch.title} `, h("span", { class: "muted" }, `${ch.summary || ""} (${where})`)),
    hasBody ? body : h("div", { class: "body muted" }, "No sentence-level detail recorded for this change."));
}

function revisionTable(rows, caption) {
  if (!rows?.length) return h("p", { class: "muted" }, `${caption}: no change-history table was found.`);
  return h("div", {}, h("p", { class: "muted" }, caption), h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Date", "Version", "Changed by", "Description"].map((t) => h("th", { scope: "col" }, t)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, h("td", { class: "nowrap" }, r.date || ""), h("td", { class: "mono" }, r.version || ""),
      h("td", {}, r.changed_by || ""), h("td", {}, r.description || "")))))));
}

function renderDiff(d) {
  const s = d.summary || {};
  const summaryTable = h("div", { class: "table-wrap diff-summary" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Entity type"), h("th", { scope: "col", class: "num" }, "Added"),
      h("th", { scope: "col", class: "num" }, "Removed"), h("th", { scope: "col", class: "num" }, "Changed"),
      h("th", { scope: "col", class: "num" }, "Possible renames"))),
    h("tbody", {}, [...TYPES, "total"].map((t) => h("tr", {},
      h("th", { scope: "row" }, t === "total" ? "Total" : t[0].toUpperCase() + t.slice(1)),
      ...["added", "removed", "changed", "possible_renames"].map((k) => h("td", { class: "num" }, s[t]?.[k] ?? 0)))))));

  const entityBlocks = TYPES.map((t) => {
    const table = entityTable(d[t] || {});
    return table ? h("details", { class: "diff-block", open: true }, h("summary", {}, h("strong", {}, t[0].toUpperCase() + t.slice(1))), table) : null;
  }).filter(Boolean);

  const sd = d.sections;
  let sections;
  if (!sd) {
    sections = h("p", { class: "muted" }, "Section comparison is not available because the processed text of one document is missing.");
  } else {
    const sm = sd.summary || {};
    const items = [["added", sd.added], ["removed", sd.removed], ["modified", sd.modified], ["front_matter", sd.front_matter]]
      .flatMap(([b, list]) => (list || []).map((ch) => sectionChange(ch, b)));
    sections = h("div", {},
      h("p", { class: "muted" }, `${sm.added || 0} added, ${sm.removed || 0} removed, ${sm.modified || 0} modified, ` +
        `${sm.front_matter || 0} front matter / change history. Expand a row for the changed sentences and table rows.`),
      items.length ? h("div", {}, items) : h("p", { class: "muted" }, "No section text changed."));
  }

  compareBox.replaceChildren(
    h("div", { class: "meta-row" }, reviewLabel(d.review_status),
      h("span", { class: "muted" }, h("span", { class: "mono" }, d.doc_a), ` (${verText(d.version_a)}) → `,
        h("span", { class: "mono" }, d.doc_b), ` (${verText(d.version_b)}). ${d.label || ""}`)),
    h("div", { class: "section" }, h("h3", {}, "Entity changes"), summaryTable,
      entityBlocks.length ? entityBlocks : h("p", { class: "muted" }, "No entity was added, removed or changed.")),
    h("div", { class: "section" }, h("h3", {}, "Section changes"), sections),
    h("details", { class: "diff-block" }, h("summary", {}, "Change-history tables"),
      revisionTable(d.revisions_a, `${d.doc_a} (${verText(d.version_a)})`), revisionTable(d.revisions_b, `${d.doc_b} (${verText(d.version_b)})`)));
}

document.getElementById("compare-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (docA.value === docB.value) {
    compareBox.replaceChildren(h("div", { class: "note error", role: "alert" }, "Both choices are the same document; choose two different versions."));
    return;
  }
  showState(compareBox, "loading", "Comparing the two versions.");
  diffBtn.disabled = true;
  try {
    const d = await api(`${P}/compare`, { method: "POST", json: { doc_a: docA.value, doc_b: docB.value } });
    lastPair = [docA.value, docB.value];
    diffBtn.disabled = false;
    diffBtn.title = `Diff ${lastPair[0]} → ${lastPair[1]}`;
    renderDiff(d);
  } catch (e) {
    showState(compareBox, "error", errorText(e, "the comparison"));
  }
});

// ------------------------------------------------------------------ exports

document.querySelectorAll("[data-export]").forEach((b) => b.addEventListener("click", async () => {
  const kind = b.dataset.export;
  const fmt = fmtSelect.value;
  exportMsg.textContent = "Preparing the file.";
  try {
    const name = kind === "diff"
      ? await download(`${P}/export/diff`, { doc_a: lastPair[0], doc_b: lastPair[1], fmt }, `diff.${fmt}`)
      : await download(`${P}/export`, { kind, fmt, doc_id: current }, `${kind}_${current}.${fmt}`);
    exportMsg.textContent = `Downloaded ${name}; every row carries its review status and page citation.`;
  } catch (e) {
    exportMsg.textContent = `The export failed (${e.message}); try again or choose another format.`;
  }
}));

// ------------------------------------------------------------------ start

function pickPair() {
  const opts = docs.map((d) => [d.doc_id, `${d.doc_id} (${verText(d.version)})`]);
  for (const sel of [docA, docB]) sel.replaceChildren(...opts.map(([v, t]) => h("option", { value: v }, t)));
  const i = docs.findIndex((d) => d.doc_id === current);
  const j = i + 1 < docs.length ? i + 1 : Math.max(i - 1, 0);
  [docA.value, docB.value] = i < j ? [docs[i].doc_id, docs[j].doc_id] : [docs[j].doc_id, docs[i].doc_id];
}

async function start() {
  try {
    docs = await loadDocuments();
  } catch (e) {
    showState(findingsBox, "error", errorText(e, "the document list"));
    return;
  }
  if (!docs.length) {
    docSelect.replaceChildren(h("option", {}, "No documents"));
    docSelect.disabled = true;
    findingsBox.replaceChildren(h("p", { class: "state" },
      "This project has no documents yet; upload one on the Documents page."));
    compareBox.replaceChildren();
    return;
  }
  current = fillDocPicker(docSelect, docs, docSelect.dataset.wanted);
  setQuery("doc", current);
  pickPair();
  docSelect.addEventListener("change", () => {
    current = docSelect.value;
    setQuery("doc", current);
    loadFindings();
  });
  loadFindings();
}

start();
