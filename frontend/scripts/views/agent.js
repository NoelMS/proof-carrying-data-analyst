// Agent model: who reads questions and writes the proofs. Install the local model, or choose rules only.
import { api } from "../api.js";
import { toast } from "../components.js";
import { arrow, h } from "../dom.js";
import { update } from "../state.js";

const MODES = [["local", "Local agent"], ["anthropic", "Claude agent"], ["none", "Rules only"]];
export const MODE_TEXT = {
  local: "A model on this PC reads each question, writes the proof code, and repairs it from the verifier's feedback.",
  anthropic: "Claude reads each question, writes the proof code, and repairs it from the verifier's feedback.",
  none: "No model: questions are read by rules and proofs come from fixed code templates, so nothing is repaired.",
};
let timer = null;

async function refreshStatus() {
  try { update({ status: await api.status() }); } catch { /* the sidebar keeps its last status */ }
}

async function install(m, rerender) {
  const what = m.ollama_installed ? m.agent_model : `Ollama (the program that runs models) and ${m.agent_model}`;
  if (!confirm(`Download and install ${what}? About 1 GB; it runs only on this PC.`)) return;
  try { await api.installModel(); } catch (e) { toast(e.message, "error"); }
  rerender();
}

async function choose(mode, rerender) {
  try {
    await api.setMode(mode);
    toast(`Now using: ${MODES.find(([v]) => v === mode)[1]}`);
  } catch (e) { toast(e.message, "error"); }
  await refreshStatus();
  rerender();
}

function progress(st) {
  return h("div", { role: "status" },
    h("div", { class: "progress" }, h("div", { class: "progress__bar", style: { "--p": String((st.percent ?? 0) / 100) } })),
    h("p", { class: "meta" }, st.message, st.percent != null ? ` · ${st.percent}%` : ""));
}

/* `compact`: the one-time choice on Home, shown only while no model is in use and no mode was chosen. */
export async function renderAgent(host, { compact = false } = {}) {
  clearTimeout(timer);
  let m;
  try { m = await api.model(); } catch { return; }
  if (!host.isConnected) return;
  const rerender = () => renderAgent(host, { compact });
  const st = m.install || {};
  if (st.running) timer = setTimeout(rerender, 1000);
  else if (st.done && m.mode === "local" && host.dataset.installing) { delete host.dataset.installing; refreshStatus(); toast("Local agent installed and in use"); }
  if (st.running) host.dataset.installing = "1";

  if (compact && (m.mode !== "none" || m.saved) && !st.running) { host.replaceChildren(); return; }
  const installBtn = !m.model_installed && h("button", { class: "btn btn--primary", type: "button", disabled: st.running || null,
    onclick: () => install(m, rerender) }, `Install local model (about 1 GB) `, arrow());
  const error = st.error && h("p", { class: "notice-inline", role: "alert" }, st.error,
    !m.ollama_installed && h("span", {}, " ", h("a", { href: m.download_url, target: "_blank", rel: "noopener" }, "Download Ollama")));

  if (compact) {
    host.replaceChildren(h("section", { class: "section agent-choice" },
      h("p", { class: "eyebrow" }, "Choose how questions are answered"),
      h("p", { class: "meta", style: { "max-width": "70ch" } },
        `No model is in use yet. ${MODE_TEXT.local} Or keep rules only: fast and fully deterministic, but it understands only the wording its rules cover.`),
      h("div", { class: "actions" },
        installBtn,
        m.model_installed && h("button", { class: "btn btn--primary", type: "button", onclick: () => choose("local", rerender) }, "Use the local agent ", arrow()),
        h("button", { class: "btn btn--secondary", type: "button", disabled: st.running || null, onclick: () => choose("none", rerender) }, "Use rules only")),
      st.running && progress(st), error));
    return;
  }

  const state = m.model_installed ? (m.ollama_running ? "installed and running" : "installed, Ollama not running")
    : m.ollama_installed ? "not downloaded yet" : "not installed (Ollama is missing)";
  host.replaceChildren(h("div", {},
    h("div", { class: "segmented", role: "group", "aria-label": "Agent mode" }, MODES.map(([value, text]) => {
      const off = (value === "local" && !m.model_installed) || (value === "anthropic" && !m.claude_available);
      return h("button", { type: "button", "aria-pressed": String(m.mode === value), disabled: off || null,
        title: off ? (value === "local" ? "Install the local model first" : "Set ANTHROPIC_API_KEY in .env") : null,
        onclick: () => m.mode !== value && choose(value, rerender) }, text);
    })),
    h("p", { class: "meta", style: { margin: "var(--space-3) 0", "max-width": "70ch" } }, MODE_TEXT[m.mode]),
    h("dl", { class: "facts" },
      h("dt", {}, "Local agent model"), h("dd", {}, `${m.agent_model} · ${state}`),
      h("dt", {}, "Fine-tuned question reader"), h("dd", {}, m.interpreter_installed
        ? `${m.interpreter_model} · installed, reads questions for the local agent`
        : `${m.interpreter_model} · not installed (optional; the agent model reads questions meanwhile)`)),
    installBtn && h("div", { class: "actions", style: { "margin-top": "var(--space-4)" } }, installBtn),
    st.running && progress(st), error));
}
