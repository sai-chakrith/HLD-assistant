import { ctx, openSettings } from "./api.js";

for (const link of document.querySelectorAll(".nav a, .brand")) {
  if (!ctx.project) continue;
  const target = new URL(link.href, location.origin);
  target.searchParams.set("project", ctx.project);
  link.href = target.pathname + target.search;
}
document.getElementById("workspace-switch")?.addEventListener("click", openSettings);

// Separate task views without duplicating document pickers or losing loaded results.
const views = [...document.querySelectorAll("[data-view]")];
const panels = [...document.querySelectorAll("[data-view-panel]")];
function selectView(name) {
  if (!views.some((button) => button.dataset.view === name)) return;
  for (const button of views) button.setAttribute("aria-pressed", String(button.dataset.view === name));
  for (const panel of panels) panel.hidden = panel.dataset.viewPanel !== name;
  const target = new URL(location.href);
  target.searchParams.set("view", name);
  history.replaceState(null, "", target);
}
for (const button of views) button.addEventListener("click", () => selectView(button.dataset.view));
const wanted = new URLSearchParams(location.search).get("view");
if (wanted) selectView(wanted);
