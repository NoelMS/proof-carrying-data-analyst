// Workbench: edit and run analysis code in the same sandbox the analyst uses. pandas, numpy and the
// standard library are already installed there; the workspace tables are mounted read-only under data/.
import { ApiError, ConnectionError, api } from "../api.js";
import { connectionLost, download, facts, marker, toast } from "../components.js";
import { arrow, h, label, setTitle } from "../dom.js";
import { store, update } from "../state.js";

const STARTER = `import json

import pandas as pd

orders = pd.read_csv("data/orders.csv", dtype=str, keep_default_na=False)
print("RESULT: " + json.dumps(len(orders)))
`;
const ALLOWED = "pandas, numpy, decimal, json, math, statistics, datetime, collections, itertools, functools, re, fractions, operator";
const referenced = (code) => [...new Set([...code.matchAll(/data\/([A-Za-z0-9_]+)\.csv/g)].map((m) => m[1]))];

export async function renderWorkbench(main, { id }) {
  setTitle("Workbench");
  let analysis = null;
  if (id) {
    try {
      analysis = await api.analysis(id);
    } catch (e) {
      if (e instanceof ConnectionError) { update({ connection: "lost" }); main.replaceChildren(connectionLost(() => location.reload())); return; }
      if (!(e instanceof ApiError && e.status === 404)) throw e;
    }
  }
  const proof = analysis?.final?.proof_code || null;
  const datasets = store.datasets || await api.datasets().catch(() => null);
  const tables = (datasets?.tables || []).map((t) => t.name);

  const input = h("textarea", { class: "editor__input", spellcheck: "false", autocapitalize: "off", autocomplete: "off",
    "aria-label": "Python code", wrap: "off" });
  input.value = proof || STARTER;
  const gutter = h("pre", { class: "editor__gutter", "aria-hidden": "true" });
  const syncGutter = () => {
    const n = input.value.split("\n").length;
    gutter.textContent = Array.from({ length: n }, (_, i) => String(i + 1).padStart(2, "0")).join("\n");
    gutter.scrollTop = input.scrollTop;
  };
  input.addEventListener("input", () => { syncGutter(); syncTables(); });
  input.addEventListener("scroll", () => { gutter.scrollTop = input.scrollTop; });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !e.shiftKey) {  // indent instead of leaving the editor; Esc then Tab moves focus on
      e.preventDefault();
      input.setRangeText("    ", input.selectionStart, input.selectionEnd, "end");
      syncGutter();
    } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      (verifyBtn || runBtn).click();
    } else if (e.key === "Escape") {
      input.blur();
    }
  });

  const chips = h("div", { class: "chips", role: "group", "aria-label": "Tables mounted under data/" },
    tables.map((t) => h("label", { class: "chip" }, h("input", { type: "checkbox", value: t }), t)));
  let manual = false;
  chips.addEventListener("change", () => { manual = true; });
  const syncTables = () => {
    if (manual) return;
    const refs = new Set(referenced(input.value));
    chips.querySelectorAll("input").forEach((c) => { c.checked = refs.has(c.value); });
  };
  const mounted = () => [...chips.querySelectorAll("input:checked")].map((c) => c.value);

  const output = h("div", { "aria-live": "polite" });
  const busy = (btn, text) => { btn.disabled = true; btn.replaceChildren(text); };
  const idle = (btn, text) => { btn.disabled = false; btn.replaceChildren(text, " ", arrow()); };

  const runBtn = h("button", { class: "btn btn--primary", type: "button" }, "Run ", arrow());
  runBtn.addEventListener("click", async () => {
    busy(runBtn, "Running…");
    try {
      const r = await api.runCode(input.value, mounted());
      output.replaceChildren(executionPanel(r.execution));
    } catch (e) {
      output.replaceChildren(errorPanel(e));
    }
    idle(runBtn, "Run");
  });

  const verifyBtn = analysis?.spec ? h("button", { class: "btn btn--primary", type: "button" }, "Run and verify ", arrow()) : null;
  verifyBtn?.addEventListener("click", async () => {
    busy(verifyBtn, "Executing and verifying…");
    try {
      const r = await api.verifyCode(id, input.value);
      output.replaceChildren(verificationPanel(r), executionPanel(r.execution));
    } catch (e) {
      output.replaceChildren(errorPanel(e));
    }
    idle(verifyBtn, "Run and verify");
  });

  main.replaceChildren(h("div", { class: "view" },
    h("header", { class: "view__head" },
      h("p", { class: "eyebrow" }, "Workbench"),
      h("h1", { class: "title", tabindex: "-1" }, analysis ? "Run the proof." : "Run your own code."),
      h("p", { class: "lead" },
        "Code runs in the analyst's sandbox, where ", h("strong", {}, ALLOWED.split(", ").slice(0, 2).join(" and ")),
        " and the standard library are already installed, so nothing has to be set up on your computer. ",
        "Tables are mounted read-only as data/<table>.csv. Print one line as RESULT: <json>."),
      analysis && h("p", { class: "meta", style: { "margin-top": "var(--space-3)" } },
        "From analysis: ", h("a", { class: "link", href: `#/analysis/${id}` }, analysis.question),
        analysis.final?.status === "verified" ? ` · shown result ${analysis.final.answer}` : "")),
    h("div", { class: "editor" }, gutter, input),
    h("div", { style: { margin: "var(--space-4) 0" } },
      h("p", { class: "eyebrow", style: { "margin-bottom": "var(--space-2)" } }, "Tables mounted under data/"),
      chips,
      h("p", { class: "meta", style: { "margin-top": "var(--space-2)" } },
        "Picked automatically from data/<table>.csv paths in the code; click to change. Allowed imports: ", ALLOWED, ".")),
    h("div", { class: "btn-row" },
      verifyBtn, runBtn,
      proof && h("button", { class: "btn btn--secondary", type: "button",
        onclick: () => { input.value = proof; manual = false; syncGutter(); syncTables(); toast("Proof restored"); } }, "Reset to proof"),
      h("button", { class: "btn btn--secondary", type: "button",
        onclick: () => download(`code-${id || "workbench"}.py`, input.value, "text/x-python") }, "Download .py"),
      analysis?.final?.status === "verified" && h("a", { class: "btn btn--secondary", href: api.bundleUrl(id) }, "Download runnable bundle (.zip)"),
      h("span", { class: "meta" }, h("span", { class: "kbd" }, "Ctrl ↵"), " run")),
    h("section", { class: "section", style: { "margin-top": "var(--space-6)" } }, output)));
  syncGutter();
  syncTables();
}

function executionPanel(ex) {
  const statusText = { ok: "Success", policy_rejected: "Blocked by policy", security_violation: "Blocked by sandbox",
    runtime_error: "Error", timeout: "Timed out", invalid_output: "No valid RESULT line", sandbox_error: "Sandbox error" }[ex.status] || ex.status;
  return h("div", { class: "tech" },
    h("div", { class: "tech__head" }, h("p", { class: "eyebrow" }, "Execution"), marker(statusText, ex.status === "ok" ? "pass" : "fail")),
    h("div", { class: "tech__body" }, facts([
      ["Status", ex.status],
      ["Duration", `${(ex.duration_s || 0).toFixed(2)} s`],
      ["Exit code", ex.exit_code == null ? "—" : String(ex.exit_code)],
      ex.result != null && ["Result", JSON.stringify(ex.result)],
      ex.error && ["Message", ex.error],
    ])),
    h("div", { class: "tech__head" }, h("p", { class: "eyebrow" }, "Output")),
    h("pre", { class: "output", tabindex: "0" }, (ex.stdout || "").trim() || "(no output)"),
    (ex.stderr || "").trim() && h("pre", { class: "output output--err", tabindex: "0" }, ex.stderr.trim()));
}

function verificationPanel(r) {
  const v = r.verification;
  const ok = v.status === "verified";
  return h("div", { style: { "margin-bottom": "var(--space-5)" } },
    h("p", { class: `verdict verdict--${ok ? "verified" : "failed"}`, style: { "margin-bottom": "var(--space-2)" } },
      ok ? "Verified" : "Verification failed"),
    h("p", { class: "meta", style: { "margin-bottom": "var(--space-3)" } }, ok
      ? (r.matches_shown_result ? "Same result as the analysis shown." : "Verified, but the value differs from the analysis shown (the data may have changed).")
      : "This code does not pass the analysis checks."),
    h("ul", { class: "checks" }, v.checks.map((c) =>
      h("li", {}, h("span", {}, label(c.name), h("span", { class: "row__sub" }, c.detail)), marker(c.passed ? "Pass" : "Fail", c.passed ? "pass" : "fail")))));
}

function errorPanel(e) {
  return h("p", { class: "notice-inline" }, e instanceof ConnectionError ? "Connection lost: the analytical service is unavailable." : e.message);
}
