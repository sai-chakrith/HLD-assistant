// Shared helpers for every page: API calls (with the X-User header), small DOM builder, display formatters,
// and the top bar (Settings popover with the project / demo-role selectors, API + model status). No business
// logic: everything shown comes from the JSON API.

const body = document.body;
export const ctx = {
  user: body.dataset.user || "",
  project: body.dataset.project || "",
  role: body.dataset.role || "",
};

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
  }
}

function url(path, params) {
  const u = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, v);
  }
  return u;
}

async function detailOf(r) {
  try {
    const j = await r.json();
    if (typeof j.detail === "string") return j.detail;
    if (Array.isArray(j.detail)) return j.detail.map((d) => d.msg).join("; ");
    return JSON.stringify(j);
  } catch {
    return r.statusText || `HTTP ${r.status}`;
  }
}

/** Call the JSON API as the selected user. Throws ApiError with the server's `detail` on failure. */
export async function api(path, { method = "GET", json, form, params, signal } = {}) {
  const headers = { "X-User": ctx.user };
  let payload;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(json);
  } else if (form) {
    payload = form;
  }
  let r;
  try {
    r = await fetch(url(path, params), { method, headers, body: payload, signal });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(0, "The API server did not respond. Check that uvicorn is running.");
  }
  if (!r.ok) throw new ApiError(r.status, await detailOf(r));
  return r.json();
}

/** Fetch a file from the API (header auth, so no plain link) and hand it to the browser as a download. */
export async function download(path, params, fallbackName) {
  const r = await fetch(url(path, params), { headers: { "X-User": ctx.user } });
  if (!r.ok) throw new ApiError(r.status, await detailOf(r));
  const cd = r.headers.get("content-disposition") || "";
  const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const name = m ? decodeURIComponent(m[1]) : fallbackName;
  const href = URL.createObjectURL(await r.blob());
  const a = h("a", { href, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
  return name;
}

/** Tiny DOM builder. Text children are inserted as text nodes, never as HTML. */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === undefined || c === null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** Replace the contents of `node` with a plain-language state line (loading / empty / error). */
export function showState(node, kind, text) {
  node.replaceChildren(h("p", { class: `state ${kind === "error" ? "error" : ""}`, role: kind === "error" ? "alert" : null }, text));
}

/** One plain sentence: what happened and what to do next. */
export function errorText(e, what) {
  if (e.status === 403) return `Your role cannot see ${what} (${e.message}); choose another role in Settings.`;
  if (e.status === 0) return `Could not load ${what} because the API server did not answer; check that uvicorn is running.`;
  return `Could not load ${what} (${e.message}); reload the page to try again.`;
}

/** Readable names for the extraction methods stored on every inventory row. */
export const METHOD_LABELS = {
  table: "from a table", diagram: "from a diagram", llm: "read by the model",
  convention: "guessed from the name", heuristic: "from headings",
};
export function methodText(m) {
  return METHOD_LABELS[m] || m || "";
}

/** "match 76%" for a retrieval similarity, with the raw value in the tooltip. */
export function matchTag(score) {
  const v = Number(score);
  if (!Number.isFinite(v)) return null;
  return h("span", { class: "tag", title: `similarity ${v.toFixed(4)}` }, `match ${Math.round(v * 100)}%`);
}

// ------------------------------------------------------------------ formatters

const SECTION_NO = /^\d+(?:\.\d+)*$/;

/** "p.9 §4.1" from either a Q&A citation (page_start, section_id) or an entity citation (page, section). */
export function citeText(c) {
  if (!c) return "";
  const page = c.page ?? c.page_start;
  let sec = c.section_id || "";
  if (!sec && c.section) {
    const first = String(c.section).split(/\s+/)[0];
    if (SECTION_NO.test(first)) sec = first;
  }
  return [page ? `p.${page}` : "", sec ? `§${sec}` : ""].filter(Boolean).join(" ");
}

/** "v1.0" for numeric versions; AUTOSAR release names such as "R3.2.3" are shown as they are. */
export function verText(v) {
  if (!v) return "";
  return /^\d/.test(String(v)) ? `v${v}` : String(v);
}

export function citeTitle(c) {
  if (!c) return "";
  const page = c.page ?? c.page_start;
  const sec = c.section || [c.section_id, c.section_title].filter(Boolean).join(" ");
  return [c.doc_id, verText(c.version), page ? `page ${page}` : "", sec].filter(Boolean).join(", ");
}

/** Small mono citation tag: [p.9 §4.1]. */
export function citeTag(c) {
  const t = citeText(c);
  return t ? h("span", { class: "cite", title: citeTitle(c) }, `[${t}]`) : h("span", { class: "tag" }, "no page");
}

/** "Confidence: High" with a one-line "why" in the tooltip (rule-based; never asked of the model). */
export function confidenceLabel(conf, why) {
  const k = String(conf || "Low").toLowerCase();
  return h("span", { class: `label ${k}`, title: why || "Rule-based: retrieval match, number of sources, share of sentences cited" },
    `Confidence: ${conf || "Low"}`);
}

/** Review status. Model-written output (answers, reports, comparisons) reads "Unreviewed AI output"; rule-based
 * issues pass ai=false and read "Not reviewed yet". */
export function reviewLabel(status, ai = true) {
  const s = status || "unreviewed";
  const text = s === "unreviewed" ? (ai ? "Unreviewed AI output" : "Not reviewed yet") : s[0].toUpperCase() + s.slice(1);
  return h("span", { class: `label ${s}` }, text);
}

export function severityLabel(sev) {
  const map = { high: "error", medium: "medium", low: "low" };
  return h("span", { class: `label ${map[sev] || "low"}` }, `Severity: ${sev || "not set"}`);
}

export function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ------------------------------------------------------------------ documents (shared by the doc pickers)

export async function loadDocuments() {
  return api(`/projects/${encodeURIComponent(ctx.project)}/documents`);
}

/** Default document: the newest release whose id starts with the project name, else the last document. */
function defaultDoc(docs) {
  const key = (ctx.project || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  const own = key ? docs.filter((d) => d.doc_id.toLowerCase().replace(/[^a-z0-9]/g, "").startsWith(key)) : [];
  const pool = own.length ? own : docs;
  const best = [...pool].sort((a, b) => String(a.version || "").localeCompare(String(b.version || ""), undefined, { numeric: true }));
  return best.length ? best[best.length - 1].doc_id : "";
}

/** Fill a <select> with documents; returns the selected doc_id (the ?doc= one when valid, else the first). */
export function fillDocPicker(select, docs, wanted) {
  select.replaceChildren(...docs.map((d) =>
    h("option", { value: d.doc_id }, `${d.title || d.doc_id} (${verText(d.version)}, ${d.source_status})`)));
  const pick = docs.some((d) => d.doc_id === wanted) ? wanted : defaultDoc(docs);
  select.value = pick;
  return pick;
}

/** Keep ?doc= in the address bar so a screen can be bookmarked or reloaded. */
export function setQuery(key, value) {
  const u = new URL(location.href);
  if (value) u.searchParams.set(key, value);
  else u.searchParams.delete(key);
  history.replaceState(null, "", u);
}

// ------------------------------------------------------------------ top bar

function setCookie(name, value) {
  document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=31536000; SameSite=Lax`;
}

/** Open the Settings popover from anywhere on the page (e.g. "Change project in Settings"). */
export function openSettings(ev) {
  ev?.stopPropagation(); // the click must not reach the "click outside closes it" handler
  const btn = document.getElementById("settings-btn");
  const box = document.getElementById("settings");
  if (btn && box?.hidden) btn.click();
}

function initSettings() {
  const btn = document.getElementById("settings-btn");
  const box = document.getElementById("settings");
  if (!btn || !box) return;
  const close = (focusBtn) => {
    if (box.hidden) return;
    box.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    if (focusBtn) btn.focus();
  };
  btn.addEventListener("click", () => {
    if (!box.hidden) { close(false); return; }
    const r = btn.getBoundingClientRect();
    box.style.top = `${Math.min(r.bottom + window.scrollY + 4, window.scrollY + window.innerHeight - 360)}px`;
    box.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    box.querySelector("select")?.focus();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(true); });
  document.addEventListener("click", (e) => { if (!box.contains(e.target) && !btn.contains(e.target)) close(false); });
}

function initSelectors() {
  document.getElementById("logout-btn")?.addEventListener("click", async () => {
    await fetch("/logout", { method: "POST" });
    location.assign("/login");
  });
  const proj = document.getElementById("project-select");
  const user = document.getElementById("user-select");
  if (ctx.project) setCookie("hld_project", ctx.project);
  proj?.addEventListener("change", () => {
    setCookie("hld_project", proj.value);
    location.assign(`${location.pathname}?project=${encodeURIComponent(proj.value)}`);
  });
  user?.addEventListener("change", () => {
    setCookie("hld_user", user.value);
    location.reload();
  });
}

async function pollHealth() {
  const dot = document.getElementById("status-dot");
  const text = document.getElementById("status-text");
  const box = document.getElementById("status");
  if (!dot) return;
  let state, label, detail;
  try {
    const r = await fetch("/health", { cache: "no-store" });
    const hl = await r.json();
    const wu = hl.warmup || {};
    const oll = hl.ollama || {};
    const emb = hl.embedder || {};
    if (wu.status === "warming") {
      [state, label, detail] = ["warming", "Warming up", "Loading the local model; the first answer may be slow."];
    } else if (!oll.reachable && hl.llm_mode === "ollama") {
      [state, label, detail] = ["down", "Offline", `The API is up but Ollama does not answer. ${oll.error || ""}`];
    } else if (wu.status === "degraded") {
      [state, label, detail] = ["down", "Offline", `Start-up check failed: ${wu.reason || "unknown reason"}`];
    } else if (emb.fallback) {
      [state, label, detail] = ["down", "Search unavailable", "Semantic search is unavailable; questions are refused. Check the embedding model setup."];
    } else if (hl.llm_mode !== "ollama") {
      [state, label, detail] = ["warming", "Evidence only", "No answer model is configured. Inventory and review tools remain available."];
    } else if ((hl.missing_models || []).includes(hl.demo_model)) {
      [state, label, detail] = ["down", "Model missing", `Install the configured answer model: ${hl.demo_model}.`];
    } else {
      [state, label, detail] = ["ready", "Ready", `Answers by ${hl.demo_model || hl.default_model}, running locally.`];
    }
  } catch {
    [state, label, detail] = ["down", "Offline", "The API server did not answer; check that uvicorn is running."];
  }
  dot.dataset.state = state;
  text.textContent = label;
  box.title = detail;
  setTimeout(pollHealth, state === "ready" ? 30000 : 5000);
}

/** Keep --bar-offset equal to the sticky bar's real height, so anchor jumps and focus never land under it. */
function trackBar() {
  const bar = document.querySelector(".topbar");
  if (!bar) return;
  const set = () => document.documentElement.style.setProperty("--bar-offset", `${getComputedStyle(bar).position === "sticky" ? bar.offsetHeight : 0}px`);
  set();
  if (window.ResizeObserver) new ResizeObserver(set).observe(bar);
}

trackBar();
initSettings();
initSelectors();
pollHealth();
