// Ask screen: one question box, sample questions built from the project's inventory, the answer with inline
// citation tags that jump to a numbered source list, confidence / limitation / review status, a distinct refusal
// block, and session history scoped to the user and project. Answers listed from the extracted inventory are shown as a table
// grouped by direction, with one citation tag per group.
import { api, citeText, citeTitle, ctx, h, loadDocuments, matchTag, reviewLabel, verText } from "./api.js";
import {
  CLOSEST_LABEL, evidenceLabel, remember, sampleAnswered, sampleCandidates, showClosest, showsModel, sourceText,
  statusText, tableLayout,
} from "./answer_view.js?v=20261016-samples";

const form = document.getElementById("ask-form");
const input = document.getElementById("question");
const btn = document.getElementById("ask-btn");
const out = document.getElementById("answer");
const histList = document.getElementById("history");
const histEmpty = document.getElementById("history-empty");
const samplesBox = document.getElementById("samples-box");
const samplesList = document.getElementById("samples");
const storageKey = `hld:ask:v1:${encodeURIComponent(ctx.user)}:${encodeURIComponent(ctx.project)}`;
let saved = {};
try {
  const value = JSON.parse(sessionStorage.getItem(storageKey) || "{}");
  if (value && typeof value === "object") saved = value;
} catch { /* An unavailable store or invalid record must not prevent asking questions. */ }
const history = (Array.isArray(saved.history) ? saved.history : []).filter((item) =>
  typeof item?.question === "string" && item.answer && typeof item.answer === "object").slice(0, 50);
let selected = history.find((item) => item.question === saved.selected) || history[0] || null;

function saveSession() {
  try {
    sessionStorage.setItem(storageKey, JSON.stringify({ history, selected: selected?.question,
      draft: input.value, expanded: document.getElementById("session-panel").open }));
  } catch {
    document.getElementById("history-storage-warning").hidden = false;
  }
}
input.addEventListener("input", saveSession);
window.addEventListener("pagehide", saveSession);
document.getElementById("session-panel").addEventListener("toggle", saveSession);

const LABEL_RE = /\[\s*(C\d+(?:\s*,\s*C\d+)*)\s*\]/g;
const ROWS_SHOWN = 8; // longer groups show this many rows and a "Show all" button

/** One line for the confidence tooltip: the server's reason when it gives one, else derived from the answer. */
function confidenceWhy(a) {
  if (a.mode === "refused") return "Refused: the documents do not support an answer.";
  if (a.confidence_reason) return `Why: ${a.confidence_reason}.`;
  const text = (a.evidence || []).filter((e) => !e.inventory).map((e) => Number(e.score)).filter(Number.isFinite);
  const top = text.length ? `top match ${Math.round(Math.max(...text) * 100)}%` : "no text match";
  const n = (a.citations || []).length;
  const sentences = (a.answer || "").split(/(?<=[.!?])\s+/).filter((x) => /\w/.test(x));
  const cited = sentences.filter((x) => /\[C\d/.test(x)).length;
  const inv = (a.evidence || []).some((e) => e.inventory) ? ", backed by the extracted inventory" : "";
  return `Why: ${top}, ${n} cited source${n === 1 ? "" : "s"}, ${cited} of ${sentences.length} sentences cited${inv}.`;
}

function evidenceBadge(a) {
  return h("span", {
    class: `label ${String(a.confidence || "Low").toLowerCase()}`,
    title: `Automated evidence rating: ${a.confidence || "Low"}. ${confidenceWhy(a)} This is not a probability of correctness.`,
  }, evidenceLabel(a));
}

/** Citation tag linking to source [n]; `page` overrides the chunk's page (a group's page). */
function citeLink(c, page) {
  const t = page ? `p.${page}` : citeText(c);
  return h("a", { class: "cite", href: `#src-${c.label}`, title: citeTitle(c) }, `[${c.n}${t ? " " + t : ""}]`);
}

/** Answer text with [C1, C3] markers replaced by tags linking to the numbered sources. */
function answerText(text, byLabel) {
  const p = h("p", { class: "answer-text" });
  let last = 0;
  for (const m of text.matchAll(LABEL_RE)) {
    p.append(text.slice(last, m.index).replace(/\s+$/, ""));
    for (const label of m[1].split(",").map((s) => s.trim())) {
      const c = byLabel.get(label);
      if (c) p.append(" ", citeLink(c)); // the server only keeps valid labels; ignore anything else
    }
    last = m.index + m[0].length;
  }
  p.append(text.slice(last));
  return p;
}

// ------------------------------------------------------------------ inventory list answers

const DIRECTION = { required: "Required", provided: "Provided" };
const MONO_KEYS = new Set(["name", "interface", "page"]);

function cellText(key, row) {
  const v = row[key];
  if (key === "page") return v ? `p.${v}` : "";
  if (key === "direction") return DIRECTION[v] || "not stated";
  if (key === "receivers") return (v || []).join(", ");
  return v || "";
}

const groupLabel = (group, cites) => [`${group.title} (${group.count})`,
  ...cites.flatMap((c) => [" ", citeLink(c, group.page)])];

function groupBody(group, cols, cites, withHead) {
  const body = h("tbody", {});
  const head = withHead
    ? h("tr", { class: "group-row" }, h("th", { scope: "rowgroup", colspan: String(cols.length) }, groupLabel(group, cites)))
    : "";
  const rowEl = (r) => h("tr", {}, cols.map(([k]) => h("td", { class: MONO_KEYS.has(k) ? "mono" : null }, cellText(k, r))));
  const fill = (all) => {
    const rows = all ? group.rows : group.rows.slice(0, ROWS_SHOWN);
    const more = !all && group.rows.length > ROWS_SHOWN
      ? h("tr", { class: "more-row" }, h("td", { colspan: String(cols.length) },
        h("button", { type: "button", class: "linkish", onclick: () => fill(true) }, `Show all ${group.rows.length}`)))
      : null;
    body.replaceChildren(head, ...rows.map(rowEl), more || "");
  };
  fill(false);
  return body;
}

function inventoryBlock(block, byLabel) {
  const cites = (block.labels || [block.label]).map((l) => byLabel.get(l)).filter(Boolean);
  const where = [block.doc_id, verText(block.version),
    [block.page ? `p.${block.page}` : "", block.section_id ? `§${block.section_id}` : ""].filter(Boolean).join(" ")]
    .filter(Boolean).join(", ");
  // groups of one block may differ in columns (ports vs signals): one table per column set
  const tables = [];
  for (const g of block.groups) {
    const key = g.columns.map((c) => c[0]).join("|");
    let t = tables.find((x) => x.key === key);
    if (!t) {
      t = { key, groups: [] };
      tables.push(t);
    }
    t.groups.push(g);
  }
  return h("div", { class: "inv-block" },
    h("p", { class: "inv-head" }, h("span", { class: "mono" }, block.subject), h("span", { class: "muted" }, ` (${where})`)),
    tables.map((t) => {
      // a single group needs no heading row: its count and citation go above the table
      const { columns, groupRows } = tableLayout(t.groups);
      return h("div", {},
        groupRows ? null : h("p", { class: "inv-group" }, groupLabel(t.groups[0], cites)),
        h("div", { class: "table-wrap" }, h("table", { class: "inv-table" },
          h("thead", {}, h("tr", {}, columns.map(([, label]) => h("th", { scope: "col" }, label)))),
          t.groups.map((g) => groupBody(g, columns, cites, groupRows)))));
    }));
}

// ------------------------------------------------------------------ sources

/** "Extracted inventory: 15 ports, §4.3, p.14" with a link to the Ports tab filtered to the component. */
function inventorySource(c, e) {
  const counts = e.inventory_counts || {};
  const parts = [["ports", counts.ports], ["signals", counts.signals]].filter(([, n]) => n)
    .map(([noun, n]) => `${n} ${n === 1 ? noun.slice(0, -1) : noun}`);
  const pages = c.page_end && c.page_end !== c.page_start ? `p.${c.page_start}-${c.page_end}` : c.page_start ? `p.${c.page_start}` : "";
  const text = ["Extracted inventory: " + (parts.join(", ") || "no rows extracted (possible extraction gap)"),
    c.section_id ? `§${c.section_id}` : "", pages].filter(Boolean).join(", ");
  const q = new URLSearchParams({ project: ctx.project, doc: c.doc_id || "", tab: counts.ports || !counts.signals ? "ports" : "signals" });
  if (e.inventory_subject) q.set("component", e.inventory_subject);
  return h("p", { class: "snippet" }, text, " ", h("a", { href: `/architecture?${q}` }, "Open in Architecture"));
}

function sourceList(citations, evidence, heading) {
  const byLabel = new Map(evidence.map((e) => [e.label, e]));
  return h("section", { class: "section" },
    h("div", { class: "section-head" }, h("h2", {}, heading)),
    h("ol", { class: "sources" }, citations.map((c) => {
      const e = byLabel.get(c.label) || {};
      const head = h("div", { class: "source-head" },
        h("span", { class: "source-n" }, `[${c.n}]`),
        h("span", { class: "mono" }, c.doc_id),
        h("span", { class: "tag" }, verText(c.version)));
      if (e.inventory) return h("li", { id: `src-${c.label}` }, head, inventorySource(c, e));
      const snip = (e.text || "").replace(/\s+/g, " ").trim();
      const pages = c.page_end && c.page_end !== c.page_start ? `p.${c.page_start}-${c.page_end}` : `p.${c.page_start}`;
      head.append(h("span", { class: "tag" }, pages),
        h("span", {}, [c.section_id ? `§${c.section_id}` : "", c.section_title || ""].filter(Boolean).join(" ")),
        (c.score > 0 ? matchTag(c.score) : ""),
        c.ocr ? h("span", { class: "label medium" }, `read by OCR${c.ocr_confidence != null ? `, ${c.ocr_confidence}% sure` : ""}`) : "");
      return h("li", { id: `src-${c.label}` }, head,
        snip ? h("details", { class: "source-excerpt" }, h("summary", {}, "Source excerpt"), h("p", { class: "snippet" }, snip.length > 600 ? snip.slice(0, 600) + "..." : snip)) : null,
        c.note ? h("p", { class: "snippet" }, c.note) : null);
    })));
}

// ------------------------------------------------------------------ answer

function renderAnswer(a) {
  const citations = (a.citations || []).map((c, i) => ({ ...c, n: i + 1 }));
  const byLabel = new Map(citations.map((c) => [c.label, c]));
  const evidence = a.evidence || [];
  const meta = h("div", { class: "meta-row" },
    evidenceBadge(a),
    h("span", { class: "muted" }, sourceText(a)),
    reviewLabel(a.review_status),
    showsModel(a) ? h("span", { class: "tag", title: "Local model that wrote part of the answer" }, a.model) : null);
  const limitation = a.limitation
    ? h("div", { class: "note warn" }, h("strong", {}, "Limitation: "), a.limitation) : null;

  if (a.mode === "refused") {
    // Refusal: no model answer. "not_extracted" says what is missing and cites the closest sections; other
    // refusals only offer the passages that were searched, collapsed and without citation tags.
    const gap = a.refusal_reason === "not_extracted";
    const closest = showClosest(a)
      ? h("details", { class: "diff-block" }, h("summary", {}, `${CLOSEST_LABEL} (${evidence.length})`),
        h("ul", { class: "sources" }, evidence.map((e) => h("li", {},
          h("div", { class: "source-head" },
            h("span", { class: "mono" }, e.doc_id || ""),
            h("span", {}, [e.section_id ? `§${e.section_id}` : "", e.inventory ? "" : e.section_title || "",
              e.page_start ? `p.${e.page_start}` : ""].filter(Boolean).join(" ")), matchTag(e.score)),
          h("p", { class: "snippet" }, e.inventory ? "Extracted inventory rows"
            : (e.text || "").replace(/\s+/g, " ").slice(0, 280))))))
      : null;
    const title = gap ? "No ports extracted for this component"
      : a.refusal_reason === "off_topic" ? "This question is outside the project's documents"
        : "Not found in the project's documents";
    out.replaceChildren(h("div", { class: "answer refused" }, meta,
      h("div", { class: "answer-body" }, h("div", { class: "answer-title" }, title),
        gap ? answerText(a.answer || "", byLabel) : h("p", { class: "answer-text" }, a.answer)),
      limitation, closest,
      gap && citations.length ? sourceList(citations, evidence, "Sources") : null));
    return;
  }

  const inv = a.answer_type === "inventory_list" && a.inventory?.blocks?.length ? a.inventory : null;
  const body = inv
    ? h("div", { class: "answer-body" }, inv.blocks.map((b) => inventoryBlock(b, byLabel)),
      inv.description ? answerText(inv.description, byLabel) : null)
    : h("div", { class: "answer-body" }, answerText(a.answer || "", byLabel));
  const parts = [meta, body, limitation];
  if (citations.length) {
    parts.push(sourceList(citations, evidence, a.mode === "evidence_only" ? "Retrieved passages" : "Sources"));
  } else {
    parts.push(h("p", { class: "muted" }, "The answer has no valid citation, so it should not be relied on."));
  }
  out.replaceChildren(h("div", { class: "answer" }, ...parts));
}

// ------------------------------------------------------------------ session list

function renderHistory() {
  document.getElementById("session-panel").hidden = history.length === 0;
  histEmpty.hidden = history.length > 0;
  histList.replaceChildren(...history.map((item) => h("li", {},
    h("button", { type: "button", onclick: () => {
      selected = item; input.value = item.question; renderAnswer(item.answer); saveSession();
    } },
      h("span", { class: "q" }, item.question),
      h("span", { class: "meta-row" }, evidenceBadge(item.answer),
        h("span", { class: "muted" }, statusText(item.answer)))))));
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault(); // Enter in the input submits the form
  const q = input.value.trim();
  if (!q || btn.disabled) return;
  saveSession();
  btn.disabled = true;
  btn.textContent = "Working…";
  out.setAttribute("aria-busy", "true");
  pauseChecks();
  samplesBox.hidden = true;
  out.replaceChildren(h("p", { class: "state" },
    "Searching the documents and writing the answer; this usually takes 5 to 10 seconds."));
  try {
    const a = await api(`/projects/${encodeURIComponent(ctx.project)}/ask`, { method: "POST", json: { question: q } });
    renderAnswer(a);
    remember(history, q, a, 50);
    selected = history[0];
    renderHistory();
    saveSession();
  } catch (e) {
    out.replaceChildren(h("div", { class: "note error", role: "alert" },
      h("strong", {}, "The question could not be answered: "), `${e.message}. Try again in a moment.`));
  } finally {
    btn.disabled = false;
    btn.textContent = "Ask";
    out.setAttribute("aria-busy", "false");
    input.focus();
    resumeChecks();
  }
});

// ------------------------------------------------------------------ sample questions from the inventory
// Candidates are checked without storing answers; only cited High-confidence answers qualify.
// Checks run one at a time and pause while the user asks.

const slots = []; // [{kind, questions, i, found}] in display order
let probe = null; // AbortController of the running check
let paused = false;
let running = false;

function renderSamples(done) {
  const found = slots.map((s) => s.found).filter(Boolean);
  samplesList.replaceChildren(...found.slice(0, 3).map((q) => h("li", {},
    h("button", { type: "button", onclick: () => { input.value = q; form.requestSubmit(); } }, q))));
  samplesBox.hidden = !found.length;
}

async function runChecks() {
  if (running) return;
  running = true;
  for (const slot of slots) {
    if (slots.filter((s) => s.found).length >= 3) break;
    while (!slot.found && slot.i < slot.questions.length && !paused) {
      probe = new AbortController();
      try {
        const a = await api(`/projects/${encodeURIComponent(ctx.project)}/ask`,
          { method: "POST", json: { question: slot.questions[slot.i], check: true }, signal: probe.signal });
        if (sampleAnswered(slot.kind, a)) slot.found = slot.questions[slot.i];
        slot.i++;
      } catch (e) {
        if (e.name === "AbortError") break; // the same candidate is checked again on resume
        slot.i++; // a candidate that fails is simply not shown
      }
      renderSamples(false);
    }
    if (paused) break;
  }
  probe = null;
  running = false;
  if (!paused) renderSamples(true);
}

function pauseChecks() {
  paused = true;
  probe?.abort();
}

function resumeChecks() {
  paused = false;
  if (slots.length) runChecks();
}

async function loadSamples() {
  let inv, docs;
  try {
    [inv, docs] = await Promise.all([api(`/projects/${encodeURIComponent(ctx.project)}/inventory`), loadDocuments()]);
  } catch {
    return; // samples are optional; the question box works without them
  }
  slots.push(...sampleCandidates(inv, docs).map((slot) => ({ ...slot, i: 0, found: "" })));
  runChecks();
}

input.value = typeof saved.draft === "string" ? saved.draft : selected?.question || "";
if (selected) renderAnswer(selected.answer);
document.getElementById("session-panel").open = Boolean(saved.expanded);
renderHistory();
loadSamples();
