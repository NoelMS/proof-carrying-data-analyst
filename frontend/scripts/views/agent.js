// Who answers: the local model or the predefined rules. Shown on the question form and the System page.
import { api } from "../api.js";
import { toast } from "../components.js";
import { h } from "../dom.js";
import { update } from "../state.js";

const OPTIONS = {
  local: { title: "Local model", sub: "AI on this PC",
    text: "A small AI model on this PC reads your question and writes the proof code, then fixes its own code until it " +
          "passes verification. Understands freer wording; takes a few seconds longer." },
  none: { title: "Predefined rules", sub: "Instant, fixed wording",
    text: "Built-in rules read your question and fixed code templates compute the answer. Instant and fully " +
          "predictable, but it only understands the wording the rules cover." },
};
let timer = null;

async function refreshStatus() {
  try { update({ status: await api.status() }); } catch { /* the sidebar keeps its last status */ }
}

async function choose(m, mode, rerender) {
  if (mode === "local" && !m.model_installed) {
    const what = m.ollama_installed ? m.agent_model : `Ollama (the program that runs it) and ${m.agent_model}`;
    if (!confirm(`The local model is not installed yet. Download and install ${what}? About 1 GB; it runs only on this PC.`)) return;
    try { await api.installModel(); } catch (e) { toast(e.message, "error"); }
    return rerender();
  }
  try {
    await api.setMode(mode);
    toast(`Questions are now answered by: ${OPTIONS[mode].title}`);
  } catch (e) { toast(e.message, "error"); }
  await refreshStatus();
  rerender();
}

/* Fills `host` with the switch; `details` adds the model names (System page). */
export async function renderAgent(host, { details = false } = {}) {
  clearTimeout(timer);
  let m;
  try { m = await api.model(); } catch { return; }
  if (!host.isConnected) return;
  const rerender = () => renderAgent(host, { details });
  const st = m.install || {};
  if (st.running) { host.dataset.installing = "1"; timer = setTimeout(rerender, 1000); }
  else if (host.dataset.installing) {
    delete host.dataset.installing;
    if (st.done) { toast("Local model installed and in use"); refreshStatus(); }
  }

  const desc = h("p", { class: "mode-switch__desc", id: `mode-desc-${details ? "sys" : "ask"}`, "aria-live": "polite" });
  const show = (mode) => {
    const o = OPTIONS[mode];
    desc.replaceChildren(h("strong", {}, o.title), " · ", o.text,
      mode === "local" && !m.model_installed ? " Not installed yet: select it to install (about 1 GB)." : "");
  };
  show(m.mode);
  const buttons = Object.entries(OPTIONS).map(([mode, o]) => h("button", {
    type: "button", class: "mode-switch__option", "aria-pressed": String(m.mode === mode), "aria-describedby": desc.id,
    title: o.text, disabled: st.running || null,
    onmouseenter: () => show(mode), onfocus: () => show(mode),
    onmouseleave: () => show(m.mode), onblur: () => show(m.mode),
    onclick: () => m.mode !== mode && choose(m, mode, rerender),
  }, h("span", { class: "mode-switch__title" }, o.title),
     h("span", { class: "mode-switch__sub" }, mode === "local" && !m.model_installed ? "Not installed · select to install" : o.sub)));

  host.replaceChildren(h("div", { class: "mode-switch" },
    h("p", { class: "eyebrow mode-switch__label" }, "Answered by"),
    h("div", { class: "mode-switch__options", role: "group", "aria-label": "Answered by" }, buttons),
    desc,
    st.running && h("div", { role: "status" },
      h("div", { class: "progress" }, h("div", { class: "progress__bar", style: { "--p": String((st.percent ?? 0) / 100) } })),
      h("p", { class: "meta" }, st.message, st.percent != null ? ` · ${st.percent}%` : "")),
    st.error && h("p", { class: "notice-inline", role: "alert" }, st.error,
      !m.ollama_installed && h("span", {}, " ", h("a", { href: m.download_url, target: "_blank", rel: "noopener" }, "Download Ollama"))),
    details && h("dl", { class: "facts", style: { "margin-top": "var(--space-4)" } },
      h("dt", {}, "Local model"), h("dd", {}, `${m.agent_model} · ${m.model_installed
        ? (m.ollama_running ? "installed and running" : "installed, Ollama not running")
        : m.ollama_installed ? "not downloaded yet" : "not installed (Ollama is missing)"}`),
      h("dt", {}, "Fine-tuned question reader"), h("dd", {}, m.interpreter_installed
        ? `${m.interpreter_model} · installed, reads questions for the local model`
        : `${m.interpreter_model} · not installed (optional)`))));
}
