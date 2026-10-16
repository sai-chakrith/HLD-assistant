// Architecture screen (read-only exploration, no decision buttons): inventory tabs with a filter, the pyvis
// dependency graph in an iframe, and a side panel with the component report and functional-flow summary.
import {
  api, citeTag, citeText, confidenceLabel, ctx, errorText, fillDocPicker, h, loadDocuments, methodText, reviewLabel,
  setQuery, showState,
} from "./api.js";

const P = `/projects/${encodeURIComponent(ctx.project)}`;
const docSelect = document.getElementById("doc-select");
const invBox = document.getElementById("inventory");
const tabs = [...document.querySelectorAll("#inv-tabs [role=tab]")];
const filter = document.getElementById("inv-filter");
const graphBox = document.getElementById("graph");
const graphCount = document.getElementById("graph-count");
const focusSel = document.getElementById("graph-focus");
const panel = document.getElementById("side-panel");
const panelTitle = document.getElementById("panel-title");
const panelBody = document.getElementById("panel-body");

const urlParams = new URLSearchParams(location.search);
let current = "";
let inventory = null;
// ?tab=ports and ?component=Name (links from Ask sources) open a tab filtered to one component
let activeType = tabs.some((t) => t.dataset.type === urlParams.get("tab")) ? urlParams.get("tab") : "components";
let component = urlParams.get("component") || "";
let lastFocus = null;
let flowCache = null; // flow summary of the current document
let panelToken = 0; // ignore responses for a panel the user already replaced

// ------------------------------------------------------------------ inventory

const compLink = (name) => (name ? h("button", { type: "button", class: "linkish mono", onclick: () => openComponent(name) }, name) : "");
const mono = (t) => h("td", { class: "mono" }, t || "");
const common = (r) => [
  h("td", { class: "nowrap" }, citeTag({ doc_id: r.doc_id, version: r.version, page: r.page, section: r.section })),
  h("td", {}, h("div", { class: "meta-row" }, h("span", { class: `label ${r.review_status || "unreviewed"}` }, r.review_status || "unreviewed"),
    !r.extraction_present ? h("span", { class: "label medium" }, "Not rediscovered") : null,
    confidenceLabel(r.confidence),
    ["reviewer", "admin"].includes(ctx.role)
      ? h("button", { class: "btn", type: "button", onclick: () => editEntity(r) }, "Review / edit") : null)),
];
const COMMON_HEAD = ["Source", "Review"];

const EDIT_FIELDS = {
  components: ["description", "category"],
  ports: ["component", "direction", "interface", "kind", "description"],
  interfaces: ["kind", "data_elements", "description"],
  signals: ["sender", "receivers", "extra_senders", "data_type", "description"],
};

function editEntity(row) {
  const type = activeType;
  openPanel(`Review ${row.name}`);
  const inputs = {};
  const message = h("p", { role: "status" });
  const fields = EDIT_FIELDS[type].map((key) => {
    const choices = key === "direction" ? ["unknown", "provided", "required"]
      : key === "kind" ? ["unknown", "sender_receiver", "client_server"] : null;
    const input = choices ? h("select", {}, choices.map((value) => h("option", { value }, value)))
      : h("input", { type: "text", value: Array.isArray(row[key]) ? row[key].join(", ") : row[key] || "" });
    if (choices) input.value = row[key] || "unknown";
    inputs[key] = input;
    return h("label", { class: "field" }, key.replaceAll("_", " "), input);
  });
  const comment = h("textarea", { required: true, rows: 3, "aria-label": "Reason for correction" });
  const save = h("button", { type: "submit", class: "btn primary" }, "Save correction");
  const form = h("form", {}, fields, h("label", { class: "field" }, "Reason for correction", comment), save, message);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const changes = {};
    for (const [key, input] of Object.entries(inputs)) {
      const val = ["receivers", "extra_senders", "data_elements"].includes(key)
        ? input.value.split(",").map((x) => x.trim()).filter(Boolean) : input.value;
      if (JSON.stringify(val) !== JSON.stringify(row[key] ?? "")) changes[key] = val;
    }
    if (!Object.keys(changes).length) { message.textContent = "Change a field before saving."; return; }
    save.disabled = true;
    try {
      await api(`${P}/corrections`, { method: "POST", json: {
        item_type: type.slice(0, -1), item_id: row.id, changes, comment: comment.value,
      } });
      flowCache = null;
      await loadInventory();
      await loadGraph();
      message.textContent = "Correction saved. The graph and checks now use the corrected facts.";
    } catch (error) { message.textContent = errorText(error, "the correction"); }
    finally { save.disabled = false; }
  });
  const decisions = ["approved", "rejected"].map((decision) => h("button", {
    type: "button", class: "btn", onclick: async () => {
      try {
        await api(`${P}/review`, { method: "POST", json: { item_type: type.slice(0, -1), item_id: row.id,
          decision, comment: comment.value } });
        flowCache = null;
        await loadInventory(); await loadGraph();
        message.textContent = `Marked ${decision}.`;
      } catch (error) { message.textContent = errorText(error, "the review"); }
    },
  }, decision === "approved" ? "Approve" : "Reject"));
  panelBody.replaceChildren(h("p", { class: "muted" },
    "Corrections persist across extraction runs. Separate multiple values with commas. Stored reports remain historical snapshots."),
    ...(row.sources || []).map((source) => h("p", {}, citeTag(source), " ", source.evidence || "No evidence text recorded")),
    form, h("div", { class: "btn-group" }, decisions));
}

const COLUMNS = {
  components: {
    head: ["Component", "Category", "Description"],
    row: (r) => [h("td", {}, compLink(r.name)), h("td", {}, r.category || ""), h("td", {}, r.description || "")],
  },
  ports: {
    head: ["Port", "Component", "Provided / Required", "Interface", "Kind"],
    row: (r) => [mono(r.name), h("td", {}, compLink(r.component)), h("td", {}, dirText(r.direction)), mono(r.interface),
      h("td", {}, kindText(r.kind))],
  },
  interfaces: {
    head: ["Interface", "Kind", "Data elements"],
    row: (r) => [mono(r.name), h("td", {}, kindText(r.kind)), h("td", { class: "mono" }, (r.data_elements || []).join(", "))],
  },
  signals: {
    head: ["Signal", "Sender", "Receivers", "Data type"],
    row: (r) => [mono(r.name), h("td", {}, compLink(r.sender)),
      h("td", {}, (r.receivers || []).flatMap((n, i) => [i ? ", " : "", compLink(n)])), mono(r.data_type)],
  },
};

function dirText(d) {
  return { provided: "Provided", required: "Required" }[d] || "not stated";
}

function kindText(k) {
  return { sender_receiver: "sender-receiver", client_server: "client-server" }[k] || "not stated";
}

function searchText(r) {
  return [r.name, r.component, r.interface, r.direction, r.category, r.description, r.sender, ...(r.receivers || []),
    r.kind, r.data_type, methodText(r.extraction_method), r.review_status, r.page && `p.${r.page}`].filter(Boolean).join(" ").toLowerCase();
}

const normName = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

/** Rows of the active type that belong to `component` (owner, sender or receiver; interfaces used by its ports). */
function ofComponent(rows, type) {
  const want = normName(component);
  const is = (name) => String(name || "").split("::").some((part) => normName(part) === want);
  if (type === "components") return rows.filter((r) => is(r.name));
  if (type === "ports") return rows.filter((r) => is(r.component));
  if (type === "signals") return rows.filter((r) => is(r.sender) || (r.receivers || []).some(is));
  const used = new Set((inventory.ports || []).filter((p) => is(p.component)).map((p) => normName(p.interface)));
  return rows.filter((r) => used.has(normName(r.name)));
}

function clearComponent() {
  component = "";
  setQuery("component", "");
  renderInventory();
}

function renderInventory() {
  if (!inventory) return;
  tabs.forEach((t) => {
    const on = t.dataset.type === activeType;
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
    if (on) invBox.setAttribute("aria-labelledby", t.id);
    t.querySelector(".count").textContent = (inventory[t.dataset.type] || []).length;
  });
  const all = component ? ofComponent(inventory[activeType] || [], activeType) : inventory[activeType] || [];
  const q = filter.value.trim().toLowerCase();
  const rows = q ? all.filter((r) => searchText(r).includes(q)) : all;
  const scope = component ? h("p", { class: "muted" }, `Showing only ${activeType} of `,
    h("span", { class: "mono" }, component), ". ",
    h("button", { type: "button", class: "linkish", onclick: clearComponent }, "Show all components")) : "";
  if (!all.length) {
    invBox.replaceChildren(scope, h("p", { class: "state" }, component
      ? `No ${activeType} of ${component} were extracted from this document.`
      : `No ${activeType} were extracted from this document; try another document.`));
    return;
  }
  if (!rows.length) {
    invBox.replaceChildren(scope, h("p", { class: "state" }, `No ${activeType} match "${filter.value}"; clear the filter to see all.`));
    return;
  }
  const col = COLUMNS[activeType];
  invBox.replaceChildren(scope, h("div", { class: "table-wrap scroll-y" }, h("table", {},
    h("thead", {}, h("tr", {}, [...col.head, ...COMMON_HEAD].map((t) => h("th", { scope: "col" }, t)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, col.row(r), common(r)))))),
    q ? h("p", { class: "muted" }, `${rows.length} of ${all.length} shown`) : "");
}

async function loadInventory() {
  showState(invBox, "loading", "Loading the inventory.");
  inventory = null;
  try {
    inventory = await api(`${P}/inventory`, { params: { doc_id: current } });
    const coverage = await api(`${P}/coverage`, { params: { doc_id: current } });
    const hasEntities = Object.values(inventory).some((rows) => rows.length);
    document.getElementById("main").classList.toggle("architecture-is-empty", !hasEntities);
    const empty = document.getElementById("architecture-empty");
    empty.hidden = hasEntities;
    empty.replaceChildren();
    if (!hasEntities) {
      empty.append(
        h("p", { class: "empty-eyebrow" }, "No extracted architecture"),
        h("h2", { id: "empty-title" }, "This document has no architecture inventory yet"),
        h("p", {}, "No components, ports, interfaces, or signals were extracted. This is an extraction gap, not confirmation that they are absent from the source."),
        h("p", { class: "muted" }, "Search the project’s documents in Ask, select another document above, or explore the public AUTOSAR architecture example."),
        h("div", { class: "btn-group" },
          h("a", { class: "btn primary", href: `/?project=${encodeURIComponent(ctx.project)}` }, "Search project documents"),
          h("a", { class: "btn", href: "/architecture?project=automotive-platform&doc=automotive-platform_bodycomfort_r3.2.3" }, "Explore AUTOSAR architecture")),
      );
    }
    const warnings = coverage.warnings.filter((text) => !text.startsWith("No "));
    const coverageBox = document.getElementById("coverage");
    coverageBox.replaceChildren(h("details", {}, h("summary", {}, "Extraction coverage"),
      ...warnings.map((text) => h("p", {}, text))));
  } catch (e) {
    showState(invBox, "error", errorText(e, "the inventory"));
    return;
  }
  const names = (inventory.components || []).map((c) => c.name).sort((a, b) => a.localeCompare(b));
  focusSel.replaceChildren(h("option", { value: "" }, "Whole document"), ...names.map((n) => h("option", { value: n }, n)));
  if (!(inventory[activeType] || []).length) {
    activeType = tabs.find((tab) => (inventory[tab.dataset.type] || []).length)?.dataset.type || "components";
  }
  renderInventory();
}

tabs.forEach((t, i) => {
  t.addEventListener("click", () => { activeType = t.dataset.type; renderInventory(); });
  t.addEventListener("keydown", (e) => { // arrow keys move between tabs (WAI-ARIA tabs pattern)
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    activeType = next.dataset.type;
    renderInventory();
    next.focus();
  });
});
filter.addEventListener("input", renderInventory);

// ------------------------------------------------------------------ graph

async function loadGraph() {
  if (graphBox.closest("[data-view-panel]").hidden) return;
  showState(graphBox, "loading", "Building the dependency graph.");
  graphCount.textContent = "";
  const params = { doc_id: current, depth: 2 };
  if (focusSel.value) params.component = focusSel.value;
  try {
    const g = await api(`${P}/graph`, { params });
    graphCount.textContent = g.component
      ? `${g.nodes} nodes, ${g.edges} edges around ${g.component} (document: ${g.total_nodes} / ${g.total_edges})`
      : `${g.nodes} nodes, ${g.edges} edges`;
    if (!g.nodes) {
      graphBox.replaceChildren(h("p", { class: "state" }, "The inventory has no entities to draw for this document."));
      return;
    }
    // The pyvis page is self-contained (vis-network inlined); sandboxed so it cannot reach this page.
    const frame = h("iframe", { class: "graph-frame", title: "Dependency graph", sandbox: "allow-scripts" });
    frame.srcdoc = g.html.replace("</body>", `<script>
      addEventListener("message", function(event) {
        if (event.source === parent && event.data === "hld-fit-graph" && typeof network !== "undefined") {
          network.fit({animation: false});
        }
      });
    </script></body>`);
    graphBox.replaceChildren(frame);
  } catch (e) {
    showState(graphBox, "error", errorText(e, "the graph"));
  }
}

const fitGraph = () => graphBox.querySelector("iframe")?.contentWindow.postMessage("hld-fit-graph", "*");
document.getElementById("graph-fit").addEventListener("click", fitGraph);
document.querySelector('[data-view="dependencies"]').addEventListener("click", () => {
  // The workspace view handler reveals the panel before this handler runs.
  if (!graphBox.querySelector("iframe") && current) loadGraph();
  else fitGraph();
});

focusSel.addEventListener("change", loadGraph);

// ------------------------------------------------------------------ side panel

function openPanel(title) {
  if (panel.hidden) lastFocus = document.activeElement;
  panelTitle.textContent = title;
  panel.hidden = false;
  document.getElementById("panel-close").focus();
  return ++panelToken;
}

function closePanel() {
  if (panel.hidden) return;
  panel.hidden = true;
  panelToken++;
  lastFocus?.focus?.();
}

document.getElementById("panel-close").addEventListener("click", closePanel);
document.addEventListener("keydown", (e) => {
  if (panel.hidden) return;
  if (e.key === "Escape") { closePanel(); return; }
  if (e.key !== "Tab") return;
  // keep keyboard focus inside the open panel
  const items = [...panel.querySelectorAll("button, a[href], select, input, [tabindex]:not([tabindex='-1'])")]
    .filter((x) => !x.disabled && x.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
  else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
  else if (!panel.contains(document.activeElement)) { first.focus(); e.preventDefault(); }
});

const LABELS = /\[\s*([A-Z]\d+(?:\s*,\s*[A-Z]\d+)*)\s*\]/g;

/** Purpose paragraph with its [F1, C2] labels turned into page tags. */
function citedText(text, cites) {
  const by = new Map((cites || []).map((c) => [c.label, c]));
  const p = h("p", {});
  let last = 0;
  for (const m of (text || "").matchAll(LABELS)) {
    p.append(text.slice(last, m.index).replace(/\s+$/, ""));
    const tags = new Set(); // one tag per distinct page/section, even when two labels point at the same place
    for (const l of m[1].split(",").map((s) => s.trim())) {
      const c = by.get(l);
      if (c && !tags.has(citeText(c))) { tags.add(citeText(c)); p.append(" ", citeTag(c)); }
    }
    last = m.index + m[0].length;
  }
  p.append((text || "").slice(last));
  return p;
}

function factTable(items, cols) {
  if (!items?.length) return h("p", { class: "muted" }, "None recorded in the inventory.");
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, [...cols.map((c) => c[0]), "Source"].map((t) => h("th", { scope: "col" }, t)))),
    h("tbody", {}, items.map((it) => h("tr", {}, cols.map(([, f]) => h("td", {}, f(it))),
      h("td", { class: "nowrap" }, citeTag(it.citation)))))));
}

function flowSteps(flows, component) {
  const want = component?.toLowerCase();
  const blocks = flows.map((fl) => {
    const steps = want ? fl.steps.filter((s) => (s.components || []).some((c) => c.toLowerCase() === want)) : fl.steps;
    if (!steps.length) return null;
    return h("div", {},
      h("p", {}, h("span", { class: "mono" }, `§${fl.section_id}`), ` ${fl.title}`),
      h("ol", { class: "flow-steps" }, steps.map((s) => h("li", { value: s.n, class: s.flagged ? "flagged" : "" },
        s.text, " ", citeTag(s.citation),
        s.flagged ? h("span", { class: "label medium", title: "Named in the flow but not in the inventory" },
          `not in inventory: ${s.unknown_entities.join(", ")}`) : null))));
  }).filter(Boolean);
  return blocks;
}

async function getFlows() {
  if (!flowCache) flowCache = await api(`${P}/reports/flow`, { params: { doc_id: current } });
  return flowCache;
}

async function openComponent(name) {
  const token = openPanel(name);
  panelBody.replaceChildren(h("p", { class: "state" },
    "Building the component report; the facts come from the inventory and the purpose is written by the local model (5 to 15 seconds)."));
  let rep;
  let flows = null;
  try {
    [rep, flows] = await Promise.all([
      api(`${P}/reports/component`, { params: { doc_id: current, component: name } }),
      getFlows().catch(() => null),
    ]);
  } catch (e) {
    if (token === panelToken) showState(panelBody, "error", errorText(e, `the report for ${name}`));
    return;
  }
  if (token !== panelToken) return;
  const flowBlocks = flows ? flowSteps(flows.flows || [], rep.component) : [];
  panelBody.replaceChildren(h("div", {},
    h("div", { class: "meta-row" }, confidenceLabel(rep.confidence,
      `Why: ${(rep.purpose_citations || []).length} cited source(s) for the purpose; facts are from the inventory.`),
      reviewLabel(rep.review_status),
      rep.model ? h("span", { class: "tag" }, rep.model) : null),
    rep.limitation ? h("div", { class: "note warn" }, h("strong", {}, "Limitation: "), rep.limitation) : null,
    h("h3", {}, "Facts"),
    h("dl", { class: "facts" },
      h("dt", {}, "Category"), h("dd", {}, rep.category || "not stated"),
      h("dt", {}, "Description"), h("dd", {}, rep.description || "not stated", " ", citeTag(rep.citation)),
      h("dt", {}, "Source pages"), h("dd", { class: "mono" }, (rep.source_pages || []).join(", ") || "none")),
    h("h3", {}, "Purpose (model-written, cited)"), citedText(rep.purpose, rep.purpose_citations),
    h("h3", {}, "Ports"), factTable(rep.ports, [["Port", (x) => h("span", { class: "mono" }, x.name)], ["Provided / Required", (x) => dirText(x.direction)],
      ["Interface", (x) => h("span", { class: "mono" }, x.interface || "")]]),
    h("h3", {}, "Interfaces"), factTable(rep.interfaces, [["Interface", (x) => h("span", { class: "mono" }, x.name)], ["Kind", (x) => kindText(x.kind)]]),
    h("h3", {}, "Depends on (upstream)"), factTable(rep.upstream, [["Component", (x) => compLink(x.name)]]),
    h("h3", {}, "Used by (downstream)"), factTable(rep.downstream, [["Component", (x) => compLink(x.name)]]),
    h("h3", {}, "Signals sent"), factTable(rep.signals_sent, [["Signal", (x) => h("span", { class: "mono" }, x.name)],
      ["Receivers", (x) => (x.receivers || []).join(", ")]]),
    h("h3", {}, "Signals received"), factTable(rep.signals_received, [["Signal", (x) => h("span", { class: "mono" }, x.name)],
      ["Sender", (x) => x.sender || ""]]),
    h("h3", {}, "Functional flows"),
    flows === null ? h("p", { class: "muted" }, "The flow summary could not be loaded; close the panel and try again.")
      : flowBlocks.length ? flowBlocks : h("p", { class: "muted" }, `${rep.component} is not named in any functional-flow section.`)));
}

async function openFlowSummary() {
  const token = openPanel("Functional-flow summary");
  showState(panelBody, "loading", "Loading the functional-flow summary.");
  let fl;
  try {
    fl = await getFlows();
  } catch (e) {
    if (token === panelToken) showState(panelBody, "error", errorText(e, "the flow summary"));
    return;
  }
  if (token !== panelToken) return;
  const blocks = flowSteps(fl.flows || []);
  panelBody.replaceChildren(h("div", {},
    h("div", { class: "meta-row" }, reviewLabel(fl.review_status),
      h("span", { class: "tag" }, `source: ${fl.source}`)),
    fl.limitation ? h("div", { class: "note warn" }, h("strong", {}, "Limitation: "), fl.limitation) : null,
    h("p", { class: "muted" }, "Steps are taken from the document's flow sections and cite their page. " +
      "Steps that name an entity missing from the inventory are marked."),
    blocks.length ? blocks : h("p", { class: "muted" }, "This document has no functional-flow section.")));
}

document.getElementById("flow-btn").addEventListener("click", openFlowSummary);

// ------------------------------------------------------------------ start

async function selectDoc(id) {
  current = id;
  flowCache = null;
  graphBox.replaceChildren();
  graphCount.textContent = "";
  document.getElementById("architecture-empty").hidden = true;
  document.getElementById("main").classList.remove("architecture-is-empty");
  setQuery("doc", current);
  closePanel();
  focusSel.value = "";
  await loadInventory();
  loadGraph();
}

async function start() {
  let docs;
  try {
    docs = await loadDocuments();
  } catch (e) {
    showState(invBox, "error", errorText(e, "the document list"));
    graphBox.replaceChildren();
    return;
  }
  if (!docs.length) {
    docSelect.replaceChildren(h("option", {}, "No documents"));
    docSelect.disabled = true;
    invBox.replaceChildren(h("p", { class: "state" }, "This project has no documents yet; upload one on the Documents page."));
    graphBox.replaceChildren();
    return;
  }
  const pick = fillDocPicker(docSelect, docs, docSelect.dataset.wanted);
  docSelect.addEventListener("change", () => selectDoc(docSelect.value));
  selectDoc(pick);
}

start();
