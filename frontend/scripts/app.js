// Application shell: routing, header status, sidebar, shortcuts, first-visit intro.
import { ConnectionError, WORKSPACES, api } from "./api.js";
import { closeDrawer, initDrawer } from "./components.js";
import { arrow, fmtInt, h } from "./dom.js";
import { initScrollEffects, reducedMotion, transition } from "./motion.js";
import { store, subscribe, update } from "./state.js";
import { OPTIONS, askOnOpen, openModePicker } from "./views/agent.js";
import { renderAnalysis } from "./views/analysis.js";
import { renderDataList, renderDataset } from "./views/data.js";
import { renderHistory } from "./views/history.js";
import { onDatasets, renderHome } from "./views/home.js";
import { renderQuality } from "./views/quality.js";
import { renderSystem } from "./views/system.js";
import { renderWorkbench } from "./views/workbench.js";

const main = document.getElementById("main");
const sidebar = document.getElementById("sidebar");
const statusEl = document.getElementById("status");
let cleanup = null;
let current = null;

/* ---------------------------------------------------------------- routing */
function parse(hash) {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  switch (parts[0]) {
    case "analysis": return parts[1] ? { route: "analysis", nav: "home", id: parts[1] } : { route: "home", nav: "home" };
    case "data": return parts[1] ? { route: "dataset", nav: "data", name: parts[1], tab: parts[2] } : { route: "data", nav: "data" };
    case "quality": return { route: "quality", nav: "quality" };
    case "history": return { route: "history", nav: "history" };
    case "system": return { route: "system", nav: "system" };
    case "workbench": return { route: "workbench", nav: "workbench", id: parts[1] };
    default: return { route: "home", nav: "home" };
  }
}

const VIEWS = {
  home: (p) => renderHome(main, p),
  analysis: (p) => renderAnalysis(main, p),
  data: (p) => renderDataList(main, p),
  dataset: (p) => renderDataset(main, p),
  quality: (p) => renderQuality(main, p),
  history: (p) => renderHistory(main, p),
  system: (p) => renderSystem(main, p),
  workbench: (p) => renderWorkbench(main, p),
};

function route({ animate = true } = {}) {
  const p = parse(location.hash);
  const same = current && current.route === p.route && current.id === p.id && current.name === p.name;
  if (same && p.route === "dataset") return; // tab changes are handled inside the inspector
  current = p;
  closeDrawer();
  document.querySelectorAll(".nav__link").forEach((a) => a.setAttribute("aria-current", a.dataset.route === p.nav ? "page" : "false"));
  const run = () => {
    cleanup?.();
    const r = VIEWS[p.route](p);
    cleanup = typeof r === "function" ? r : null;
    window.scrollTo(0, 0);
  };
  const done = animate ? transition(run) : Promise.resolve(run());
  done.then(() => main.querySelector("h1[tabindex='-1']")?.focus({ preventScroll: true }));
}

/* ---------------------------------------------------------------- header status (always backed by real state) */
function renderStatus(s) {
  let state = "idle", text = "Connecting";
  if (s.connection === "lost") { state = "error"; text = "Connection lost"; }
  else if (s.activity?.busy) { state = "busy"; text = s.activity.label; }
  else if (s.status && !s.status.sandbox.ready) { state = "error"; text = "Sandbox error"; }
  else if (s.status) { state = "ready"; text = "System ready"; }
  statusEl.dataset.state = state;
  statusEl.querySelector(".status__text").textContent = text;
}

/* ---------------------------------------------------------------- sidebar */
function renderSidebar(s) {
  const group = (title, ...children) => h("section", { class: "sidebar__group" }, h("h2", { class: "eyebrow" }, title), ...children);
  const here = decodeURIComponent(location.hash || "#/").replace(/\/(overview|schema|quality|sample|relationships)$/, "");
  const item = (href, left, right, truncate, title) => h("li", {}, h("a", {
    class: `sidebar__item${truncate ? " sidebar__item--truncate" : ""}`, href, title,
    "aria-current": decodeURIComponent(href) === here ? "page" : null },
    h("span", {}, left), right != null && h("span", { class: "num" }, right)));
  const mode = OPTIONS[s.status?.mode];
  sidebar.replaceChildren(
    group("Answered by", h("button", { type: "button", class: "sidebar__item sidebar__mode", id: "sidebar-mode",
      title: mode ? `${mode.text} Click to change.` : "Choose how questions are answered", onclick: openModePicker },
      h("span", { class: "sidebar__mode-name" }, mode?.title || "Choose…"),
      h("span", { class: "sidebar__mode-sub" }, mode ? mode.sub : "How questions are answered"))),
    group(`Data · ${WORKSPACES[s.datasets?.workspace] || WORKSPACES.demonstration}`, s.datasets
      ? h("ul", { class: "sidebar__list" }, s.datasets.tables.map((t) => item(`#/data/${encodeURIComponent(t.name)}`, t.name, fmtInt(t.rows))))
      : h("p", { class: "meta" }, s.connection === "lost" ? "Unavailable" : "Loading")),
    group("Analysis",
      h("ul", { class: "sidebar__list" },
        item("#/", h("span", {}, "New question ", arrow())),
        (s.history || []).slice(0, 6).map((it) => item(`#/analysis/${it.id}`, it.question, it.status === "verified" ? "V" : it.status === "refused" ? "R" : "…", true,
          `${it.question} (${it.status || "running"})`))),
      s.history?.length > 6 && h("a", { class: "link meta", href: "#/history" }, "All history")),
    group("System", s.status
      ? h("ul", { class: "sidebar__list" },
          item("#/system", "Sandbox", s.status.sandbox.ready ? "ready" : "error"),
          item("#/quality", "Data issues", s.datasets ? String(s.datasets.issues.length) : "—"))
      : h("p", { class: "meta" }, "—")),
  );
}

/* ---------------------------------------------------------------- data loading */
async function load() {
  try {
    const [status, datasets, history] = await Promise.all([api.status(), api.datasets(), api.history()]);
    update({ status, datasets, history, connection: "ok" });
  } catch (e) {
    update({ connection: e instanceof ConnectionError ? "lost" : "ok" });
  }
}

/* ---------------------------------------------------------------- shortcuts */
function initShortcuts() {
  addEventListener("keydown", (e) => {
    if (e.key === "Escape" && closeDrawer()) { e.preventDefault(); return; }
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
    if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      const q = document.getElementById("question");
      if (q) q.focus();
      else { location.hash = "#/"; setTimeout(() => document.getElementById("question")?.focus(), 500); }
    }
  });
}

/* ---------------------------------------------------------------- intro (first visit per session, 3 s) */
function intro() {
  let seen = true;
  try { seen = sessionStorage.getItem("pcda-intro") === "1"; sessionStorage.setItem("pcda-intro", "1"); } catch { /* storage unavailable */ }
  if (seen || reducedMotion()) return false;
  const el = document.getElementById("intro");
  el.hidden = false;
  setTimeout(() => el.classList.add("is-leaving"), 2500);
  setTimeout(() => { el.hidden = true; }, 3000);
  return true;
}

/* ---------------------------------------------------------------- theme (dark by default) */
function initTheme() {
  const btn = document.getElementById("theme-toggle");
  const sync = () => {
    const light = document.documentElement.dataset.theme === "light";
    btn.textContent = light ? "Dark theme" : "Light theme";
    btn.setAttribute("aria-pressed", String(light));
  };
  btn.addEventListener("click", () => {
    const light = document.documentElement.dataset.theme !== "light";
    if (light) document.documentElement.dataset.theme = "light";
    else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("pcda-theme", light ? "light" : "dark"); } catch { /* not persisted */ }
    sync();
  });
  sync();
}

/* ---------------------------------------------------------------- boot */
// views that read the store re-render when the data they show arrives or changes
const DEPENDS = { data: "datasets", dataset: "datasets", quality: "datasets", system: "status", history: "history" };
const seen = { datasets: null, status: null, history: null };
subscribe((s) => {
  renderStatus(s);
  renderSidebar(s);
  if (s.datasets !== seen.datasets && current?.route === "home") onDatasets(s.datasets);
  const dep = DEPENDS[current?.route];
  const changed = dep && s[dep] !== seen[dep];
  Object.keys(seen).forEach((k) => { seen[k] = s[k]; });
  if (changed) { current = null; route({ animate: false }); }
});

askOnOpen(intro() ? 3100 : 400);  // the answering choice follows the intro
initTheme();
initDrawer();
initShortcuts();
initScrollEffects(document.getElementById("header"), document.getElementById("scroll-progress"));
addEventListener("hashchange", () => { route(); renderSidebar(store); });
route({ animate: false });
renderStatus(store);
renderSidebar(store);
load();
