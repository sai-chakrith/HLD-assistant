// Display rules of the Ask screen that do not touch the DOM (kept apart so they can be tested with node).

/** Describe the answer's evidence without presenting the heuristic rating as a probability. */
export function evidenceLabel(a) {
  if (a.mode === "refused") return "Answer withheld";
  if (a.mode === "evidence_only") return "Retrieved passages only";
  const count = (a.citations || []).length;
  if (!count) return "No cited evidence";
  if (String(a.confidence).toLowerCase() === "low") return "Limited evidence";
  if (a.built_from === "inventory" || a.built_from === "inventory+llm") {
    return a.confidence === "High" ? "Facts corroborated" : "Extraction needs verification";
  }
  return count === 1 ? "Based on one cited passage" : `Based on ${count} cited passages`;
}

/** Who wrote the answer text. */
export function sourceText(a) {
  if (a.mode === "refused") return "Refused";
  if (a.mode === "evidence_only") return "Passages only (no local model)";
  return {
    inventory: "Built from extracted inventory",
    "inventory+llm": "Built from extracted inventory; description written by the model",
    change_history: "Built from the change-history table",
  }[a.built_from] || "Generated answer";
}

/** The model name is shown only when the model wrote some of the answer text. */
export function showsModel(a) {
  return Boolean(a.model) && a.mode === "generated" && (a.built_from === "llm" || a.built_from === "inventory+llm");
}

const key = (q) => String(q || "").trim().replace(/\s+/g, " ").toLowerCase();

/** Session list, newest first, max 5: the same question asked again right away replaces its entry. */
export function remember(list, question, answer, limit = 5) {
  if (list.length && key(list[0].question) === key(question)) list[0] = { question, answer };
  else list.unshift({ question, answer });
  list.splice(limit);
  return list;
}

/** Session-list status for every entry. */
export function statusText(a) {
  return a?.mode === "refused" ? "Refused" : "Answered";
}

/** Columns of one inventory table (Page dropped when no row has a page of its own) and whether each group needs
 * its own heading row (only when the table holds more than one group). */
export function tableLayout(groups) {
  const cols = groups[0]?.columns || [];
  const hasPage = groups.some((g) => g.rows.some((r) => r.page));
  return { columns: cols.filter(([k]) => k !== "page" || hasPage), groupRows: groups.length > 1 };
}

export const OFF_TOPIC = "What will the weather be like tomorrow?"; // a deliberate refusal: no HLD covers it
const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

/** Suggestions require an actual cited High-confidence answer; inventory questions also require structured rows. */
export function sampleAnswered(kind, a) {
  return a?.mode === "generated" && a.confidence === "High" && (a.citations || []).length > 0
    && Boolean(a.answer?.trim()) && (["section", "history"].includes(kind) || a.answer_type === "inventory_list");
}

/** Candidate sample questions per slot ({kind, questions}, best first): each slot shows its first candidate that
 * gets an answer. Ports questions only for components that have extracted ports. */
export function sampleCandidates(inv, docs = []) {
  const comps = inv.components || [];
  const portsOf = new Map();
  for (const p of inv.ports || []) {
    for (const part of String(p.component || "").split("::")) {
      const k = norm(part);
      if (k) portsOf.set(k, (portsOf.get(k) || 0) + 1);
    }
  }
  const withPorts = comps.filter((c) => portsOf.has(norm(c.name)))
    .sort((a, b) => portsOf.get(norm(b.name)) - portsOf.get(norm(a.name)));
  const names = [...new Set(withPorts.map((c) => c.name))].slice(0, 3);
  const sections = [...new Set([...withPorts, ...comps].map((c) => String(c.section || "").trim()).filter(Boolean))]
    .slice(0, 2); // the top component's section first
  const signals = [...new Set((inv.signals || []).filter((x) => x.sender).map((x) => x.name))].slice(0, 2);
  return [
    ...docs.slice(0, 3).map((d) => ({ kind: "history", questions: [
      `What is the latest change history entry in ${d.doc_id}?`,
    ] })),
    { kind: "ports", questions: names.map((n) => `Which ports does the ${n} have?`) },
    { kind: "section", questions: sections.map((s) => `What does section ${s} describe?`) },
    { kind: "signal", questions: signals.map((s) => `Who sends ${s} and who receives it?`) },
  ].filter((slot) => slot.questions.length);
}

export const CLOSEST_LABEL = "Closest passages searched (not used in an answer)";

/** The searched passages are offered (collapsed) for "not found" refusals only: not for off-topic questions and
 * not when the refusal already cites the closest sections ("not_extracted"). */
export function showClosest(a) {
  return a?.mode === "refused" && (a.evidence || []).length > 0
    && a.refusal_reason !== "off_topic" && a.refusal_reason !== "not_extracted";
}
