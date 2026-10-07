// Who answers: the local model or the predefined rules. Chosen in a popup when the app opens, which then
// minimizes into the sidebar's "Answered by" item (click it to choose again); also on the System page.
import { api } from "../api.js";
import { toast } from "../components.js";
import { h } from "../dom.js";
import { reducedMotion } from "../motion.js";
import { update } from "../state.js";

export const OPTIONS = {
  local: { title: "Local model", sub: "AI on this PC",
    text: "A small AI model on this PC reads your question and writes the proof code, then fixes its own code until it " +
          "passes verification. Understands freer wording; takes a few seconds longer." },
  none: { title: "Predefined rules", sub: "Instant, fixed wording",
    text: "Built-in rules read your question and fixed code templates compute the answer. Instant and fully " +
          "predictable, but it only understands the wording the rules cover." },
};

async function refreshStatus() {
  try { update({ status: await api.status() }); } catch { /* the sidebar keeps its last status */ }
}

/* The option cards plus a description that follows hover, focus and the current choice. */
function options(m, current, onPick) {
  const desc = h("p", { class: "mode-switch__desc", "aria-live": "polite" });
  const show = (mode) => desc.replaceChildren(h("strong", {}, OPTIONS[mode].title), " · ", OPTIONS[mode].text,
    mode === "local" && !m.model_installed ? " Not installed yet: select it to install (about 1 GB)." : "");
  show(current);
  const buttons = Object.entries(OPTIONS).map(([mode, o]) => h("button", {
    type: "button", class: "mode-switch__option", "data-mode": mode, "aria-pressed": String(current === mode),
    title: o.text, disabled: m.install?.running || null,
    onmouseenter: () => show(mode), onfocus: () => show(mode), onmouseleave: () => show(current), onblur: () => show(current),
    onclick: (e) => onPick(mode, e.currentTarget),
  }, h("span", { class: "mode-switch__title" }, o.title),
     h("span", { class: "mode-switch__sub" }, mode === "local" && !m.model_installed ? "Not installed · select to install" : o.sub)));
  return [h("div", { class: "mode-switch__options", role: "group", "aria-label": "Answered by" }, buttons), desc];
}

function installStatus(st, m) {
  return [
    st.running && h("div", { role: "status" },
      h("div", { class: "progress" }, h("div", { class: "progress__bar", style: { "--p": String((st.percent ?? 0) / 100) } })),
      h("p", { class: "meta" }, st.message, st.percent != null ? ` · ${st.percent}%` : "")),
    st.error && h("p", { class: "notice-inline", role: "alert" }, st.error,
      !m.ollama_installed && h("span", {}, " ", h("a", { href: m.download_url, target: "_blank", rel: "noopener" }, "Download Ollama"))),
  ];
}

/* Starts the install after confirmation; resolves true once the local model is installed and in use. */
async function install(m) {
  const what = m.ollama_installed ? m.agent_model : `Ollama (the program that runs it) and ${m.agent_model}`;
  if (!confirm(`The local model is not installed yet. Download and install ${what}? About 1 GB; it runs only on this PC.`)) return false;
  try { await api.installModel(); } catch (e) { toast(e.message, "error"); return false; }
  return true;
}

async function select(m, mode) {
  try { await api.setMode(mode); } catch (e) { toast(e.message, "error"); return false; }
  await refreshStatus();
  return true;
}

/* ---------------------------------------------------------------- popup */
let open = false;

export async function openModePicker() {
  if (open) return;
  let m;
  try { m = await api.model(); } catch { return; }
  open = true;
  const panel = h("div", { class: "mode-dialog__panel" });
  const dialog = h("dialog", { class: "mode-dialog", "aria-labelledby": "mode-dialog-title" }, panel);
  document.body.append(dialog);

  const render = () => panel.replaceChildren(...[
    h("p", { class: "eyebrow" }, "Before you ask"),
    h("h2", { class: "mode-dialog__title", id: "mode-dialog-title" }, "How should questions be answered?"),
    h("p", { class: "meta" }, "Either way, every number is executed and independently verified before it is shown. You can change this any time from the sidebar."),
    ...options(m, m.mode, pick),
    ...installStatus(m.install || {}, m)].filter(Boolean));

  async function waitForInstall(card) {
    for (;;) {
      await new Promise((r) => setTimeout(r, 1000));
      try { m = await api.model(); } catch { continue; }
      render();
      if (!m.install?.running) break;
    }
    if (m.install?.done && m.mode === "local") { await refreshStatus(); minimize(dialog, panel.querySelector('[data-mode="local"]') || card); }
  }

  async function pick(mode, card) {
    if (mode === "local" && !m.model_installed) {
      if (await install(m)) { m.install = { running: true, message: "Starting…" }; render(); waitForInstall(card); }
      return;
    }
    if (mode !== m.mode && !(await select(m, mode))) return;
    minimize(dialog, card);
  }

  render();
  dialog.addEventListener("cancel", (e) => {  // Escape keeps the current choice
    e.preventDefault();
    if (!m.install?.running) minimize(dialog, panel.querySelector(`[data-mode="${m.mode}"]`));
  });
  dialog.showModal();
  dialog.animate([{ opacity: 0, transform: "translateY(12px) scale(0.98)" }, { opacity: 1, transform: "none" }],
    { duration: reducedMotion() ? 1 : 320, easing: "cubic-bezier(0.2, 0.7, 0.1, 1)" });
}

/* The chosen card shrinks into the sidebar item while the popup fades away. */
function minimize(dialog, card) {
  const done = () => { dialog.close(); dialog.remove(); open = false; };
  const target = document.getElementById("sidebar-mode");
  const to = target?.getBoundingClientRect();
  if (reducedMotion() || !card || !to || !to.width || to.top > innerHeight || to.bottom < 0) {  // no visible target (e.g. phone): fade
    dialog.animate([{ opacity: 1 }, { opacity: 0 }], { duration: reducedMotion() ? 1 : 200 }).onfinish = done;
    return;
  }
  const from = card.getBoundingClientRect();
  const ghost = card.cloneNode(true);
  ghost.classList.add("mode-ghost");
  Object.assign(ghost.style, { left: `${from.left}px`, top: `${from.top}px`, width: `${from.width}px`, height: `${from.height}px` });
  dialog.append(ghost);  // inside the dialog, so it stays above the backdrop while it travels
  card.style.visibility = "hidden";
  const ease = "cubic-bezier(0.65, 0, 0.2, 1)";
  dialog.querySelector(".mode-dialog__panel").animate([{ opacity: 1 }, { opacity: 0 }], { duration: 260, fill: "forwards" });
  dialog.animate([{ opacity: 1 }, { opacity: 0 }], { pseudoElement: "::backdrop", duration: 600, easing: ease, fill: "forwards" });
  ghost.animate([
    { transform: "none", opacity: 1 },
    { transform: `translate(${to.left - from.left}px, ${to.top - from.top}px) scale(${to.width / from.width}, ${to.height / from.height})`, opacity: 0.35 },
  ], { duration: 650, easing: ease, fill: "forwards" }).onfinish = () => {
    done();
    target.animate([{ backgroundColor: "var(--color-surface)", boxShadow: "inset 0 0 0 1px var(--color-accent)" }, { boxShadow: "none" }],
      { duration: 900, easing: "ease-out" });
  };
}

/* Opens the popup once each time the app is opened (a new app window is a new session). */
export function askOnOpen(delay) {
  let asked = true;
  try { asked = sessionStorage.getItem("pcda-mode-asked") === "1"; sessionStorage.setItem("pcda-mode-asked", "1"); } catch { /* storage unavailable */ }
  if (!asked) setTimeout(openModePicker, delay);
}

/* ---------------------------------------------------------------- System page panel */
let timer = null;

export async function renderAgent(host) {
  clearTimeout(timer);
  let m;
  try { m = await api.model(); } catch { return; }
  if (!host.isConnected) return;
  const rerender = () => renderAgent(host);
  const st = m.install || {};
  if (st.running) timer = setTimeout(rerender, 1000);
  const pick = async (mode) => {
    if (mode === m.mode) return;
    if (mode === "local" && !m.model_installed) { await install(m); return rerender(); }
    if (await select(m, mode)) toast(`Questions are now answered by: ${OPTIONS[mode].title}`);
    rerender();
  };
  host.replaceChildren(h("div", { class: "mode-switch" },
    ...options(m, m.mode, pick),
    ...installStatus(st, m),
    h("dl", { class: "facts", style: { "margin-top": "var(--space-4)" } },
      h("dt", {}, "Local model"), h("dd", {}, `${m.agent_model} · ${m.model_installed
        ? (m.ollama_running ? "installed and running" : "installed, Ollama not running")
        : m.ollama_installed ? "not downloaded yet" : "not installed (Ollama is missing)"}`),
      h("dt", {}, "Fine-tuned question reader"), h("dd", {}, m.interpreter_installed
        ? `${m.interpreter_model} · installed, reads questions for the local model`
        : `${m.interpreter_model} · not installed (optional)`))));
}
