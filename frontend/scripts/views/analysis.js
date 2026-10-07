// Analysis workspace: stable analytical record on the left, transitioning state on the right.
// Everything rendered here comes from backend snapshots streamed after each workflow stage.
import { ApiError, ConnectionError, api, streamAnalysis } from "../api.js";
import { barChart } from "../charts.js";
import { announce, codeBlock, connectionLost, copyText, facts, marker, openDrawer, table, toast } from "../components.js";
import { append, arrow, fmtInt, h, label, pad, setTitle } from "../dom.js";
import { animateNumber, enter, reveal, swap } from "../motion.js";
import { store, update } from "../state.js";
import { openFixDrawer } from "./fixes.js";

const ORDER = ["interpret", "assess", "plan", "generate", "execute", "verify"];
const WORK = {
  interpret: "Interpreting the question",
  assess: "Checking data quality and answerability",
  plan: "Constructing the analytical plan",
  generate: "Generating executable proof",
  execute: "Executing the proof in the sandbox",
  verify: "Reproducing and independently recomputing",
};
const HEADLINE = {
  interpret: "Reading the question", assess: "Checking the data", plan: "Planning the analysis",
  generate: "Writing the proof", execute: "Executing the proof", verify: "Verifying the result",
};
export const STATE_LABEL = {
  ANSWERABLE: "Answerable", AMBIGUOUS: "Ambiguous", INSUFFICIENT_DATA: "Insufficient data",
  CONTRADICTORY_DATA: "Contradictory data", UNSUPPORTED_OPERATION: "Unsupported operation",
  VERIFICATION_FAILED: "Verification failed", REFUSE: "Refused",
};
const AGG = { sum: "Total", mean: "Average", median: "Median", count: "Count", count_distinct: "Distinct count" };

/* ---------------------------------------------------------------- derived state */
function nextStage(s) {
  if (s.final) return null;
  return s.stages.at(-1)?.next ?? "interpret";
}

function phase(s) {
  if (s.final) return s.final.status === "verified" ? "verified" : "refused";
  const next = nextStage(s);
  if (s.attempts.length && ["generate", "execute", "verify"].includes(next) && failedAttempt(s)) return "repairing";
  if (next === "execute") return "executing";
  if (next === "verify") return "verifying";
  return "analyzing";
}

const failedAttempt = (s) => [...s.attempts].reverse().find((a) => a.verification && a.verification.status === "failed");
const lastAttempt = (s) => s.attempts.at(-1);
const cap = (t) => (t ? t[0].toUpperCase() + t.slice(1) : "");
const fmtVal = (v) => {
  if (v == null) return "—";
  const entries = Array.isArray(v) ? v : typeof v === "object" ? Object.entries(v) : null;
  if (!entries) return String(v);
  return entries.length === 1 ? entries[0].join(": ") : `${entries.length} values`;
};

function describeMetric(s) {
  const sp = s.spec || {};
  const parts = [];
  if (sp.ratio_filter) parts.push(cap(sp.metric_term || "rate"));
  else if (sp.growth_from) parts.push(`${cap(sp.metric_term)} growth, ${sp.growth_from} to ${sp.growth_to}`);
  else parts.push(`${AGG[sp.aggregation || "sum"]} ${sp.metric_term || ""}`.trim());
  for (const f of sp.filters || []) parts.push(`${label(f.column.split(".").pop())} = ${f.value}`);
  if (sp.group_by) parts.push(`by ${label(sp.group_by.split(".").pop())}`);
  if (sp.time_grain) parts.push(`per ${sp.time_grain}`);
  if (sp.top_n) parts.push(sp.top_n === 1 ? (sp.order === "asc" ? "lowest" : "highest") : `${sp.order === "asc" ? "bottom" : "top"} ${sp.top_n}`);
  if (sp.date_from) parts.push(`${sp.date_from} to ${sp.date_to}`);
  if (s.plan?.unit) parts.push(s.plan.unit);
  return parts.join(" · ");
}

/* ---------------------------------------------------------------- view */
export function renderAnalysis(main, { id }) {
  setTitle("Analysis");
  const slots = {
    question: h("div", { class: "panel-block" }),
    interp: h("div", { class: "panel-block" }),
    timeline: h("ol", { class: "timeline", "aria-label": "Analysis progress" }),
    data: h("div", { class: "panel-block" }),
    checks: h("div", { class: "panel-block" }),
    plan: h("div", { class: "panel-block" }),
    verification: h("div", { class: "panel-block" }),
  };
  const stageHost = h("div", { class: "split__right", "aria-busy": "true" });
  const after = h("div");
  main.replaceChildren(h("div", { class: "view view--analysis" },
    h("div", { class: "view__head" },
      h("p", { class: "eyebrow" }, "Analysis ", h("span", { class: "mono" }, id))),
    h("div", { class: "split" },
      h("div", { class: "split__left" }, h("div", { class: "split__sticky" },
        slots.question, slots.interp, slots.timeline, slots.data, slots.checks, slots.plan, slots.verification)),
      stageHost),
    after));

  const pending = store.pending?.id === id ? store.pending.question : null;
  if (pending) fillQuestion(slots.question, pending);

  let key = null, snap = null, closed = false, pollTimer = null;
  const filled = new Set();

  const render = (s) => {
    if (closed) return;
    snap = s;
    renderLeft(s, slots, filled);
    const ph = phase(s);
    const k = ph === "repairing" ? `${ph}-${s.attempts.length}` : ph;
    const stage = renderStage(s, ph, id);
    if (k !== key) {
      swap(stageHost, stage);
      if (key !== null || s.final) announce(stageAnnouncement(s, ph));
      key = k;
    } else {
      stageHost.replaceChildren(stage);
      stage.classList.add("is-entered");
    }
    if (s.final) {
      stageHost.setAttribute("aria-busy", "false");
      renderAfter(after, s, id);
      update({ activity: null });
    } else {
      update({ activity: { busy: true, label: { analyzing: "Analyzing", executing: "Executing", verifying: "Verifying", repairing: "Repairing" }[ph] } });
    }
  };

  const showLost = () => {
    if (closed) return;
    update({ activity: null, connection: "lost" });
    main.replaceChildren(h("div", { class: "view" }, connectionLost(() => { update({ connection: "ok" }); location.reload(); })));
  };

  const poll = async () => {  // fallback when the event stream drops before completion
    try {
      const s = await api.analysis(id);
      render(s);
      if (!s.final && !closed) pollTimer = setTimeout(poll, 1200);
    } catch (e) {
      if (e instanceof ConnectionError) showLost();
      else if (e instanceof ApiError && e.status === 404) notFound();
    }
  };

  const notFound = () => {
    main.replaceChildren(h("div", { class: "view" }, h("section", { class: "notice" },
      h("p", { class: "eyebrow" }, "Analysis not found"),
      h("h1", { class: "section-title" }, "This analysis is not available."),
      h("p", {}, "It may belong to a previous session whose history was cleared."),
      h("a", { class: "btn btn--primary", href: "#/" }, "Ask a question ", arrow()))));
  };

  const stop = streamAnalysis(id, {
    onState: render,
    onEnd: () => { if (!snap) poll(); refreshHistory(); },
    onError: () => { if (!snap?.final) poll(); },
  });

  if (!pending) stageHost.replaceChildren(h("div", { class: "stage" }, h("p", { class: "eyebrow" }, "Loading analysis")));
  else render({ id, question: pending, stages: [], attempts: [], datasets: [], issues: [], final: null });

  return () => {
    closed = true;
    stop();
    clearTimeout(pollTimer);
    if (!snap?.final) update({ activity: null });
  };
}

function refreshHistory() {
  api.history().then((history) => update({ history })).catch(() => {});
}

function stageAnnouncement(s, ph) {
  if (ph === "verified") return `Verified result. ${s.final.answer}`;
  if (ph === "refused") return `Cannot determine. ${s.final.reason}`;
  if (ph === "repairing") return "Verification failed. Repairing the analysis.";
  return HEADLINE[nextStage(s)] || "Analyzing";
}

/* ---------------------------------------------------------------- left: stable record */
function fillQuestion(slot, question) {
  slot.replaceChildren(h("p", { class: "eyebrow" }, "Question"), h("h1", { class: "question-text", tabindex: "-1" }, question));
}

function renderLeft(s, slots, filled) {
  if (s.question && !filled.has("question")) { fillQuestion(slots.question, s.question); filled.add("question"); }
  renderTimeline(slots.timeline, s);

  const once = (name, slot, build) => {
    const content = build();
    if (!content) return;
    slot.replaceChildren();
    append(slot, content);
    if (!filled.has(name)) { reveal(slot); filled.add(name); }
  };
  once("interp", slots.interp, () => s.spec && [
    h("p", { class: "eyebrow" }, "Read as"),
    h("p", {}, describeMetric(s) || s.spec.metric_term),
    s.spec.notes?.length > 0 && h("ul", { class: "interpretation", style: { "margin-top": "var(--space-2)" } },
      s.spec.notes.map((n) => h("li", {}, n))),
  ]);
  once("data", slots.data, () => s.datasets.length && [
    h("p", { class: "eyebrow" }, "Data used"),
    h("ul", { class: "datasets-used" }, s.datasets.map((d) =>
      h("li", {}, h("a", { class: "link", href: `#/data/${encodeURIComponent(d.name)}` }, d.name),
        h("span", { class: "num meta" }, `${fmtInt(d.rows)} rows`)))),
  ]);
  once("checks", slots.checks, () => s.answerability && [
    h("p", { class: "eyebrow" }, "Data checks"),
    h("p", {}, marker(STATE_LABEL[s.answerability.status] || s.answerability.status,
      s.answerability.status === "ANSWERABLE" ? "pass" : "fail")),
    h("ul", { class: "notes" }, [...s.answerability.reasons, ...s.answerability.diagnostics].map((d) => h("li", {}, d))),
  ]);
  once("plan", slots.plan, () => s.plan && [
    h("p", { class: "eyebrow" }, "Analytical plan"),
    h("ol", { class: "plan" }, s.plan.steps.map((st) => h("li", {}, st))),
  ]);
  const v = lastAttempt(s)?.verification;
  once("verification", slots.verification, () => v && [
    h("p", { class: "eyebrow" }, `Verification${s.attempts.length > 1 ? ` · attempt ${s.attempts.length}` : ""}`),
    h("ul", { class: "checks" }, v.checks.map((c) =>
      h("li", {}, h("span", {}, label(c.name)), marker(c.passed ? "Pass" : "Fail", c.passed ? "pass" : "fail")))),
  ]);
}

function renderTimeline(ol, s) {
  const done = new Set(s.stages.map((e) => e.stage));
  const next = nextStage(s);
  const refusedEarly = s.final?.status === "refused" && !s.attempts.length;
  const verifyFailed = s.final?.answerability === "VERIFICATION_FAILED";
  const state = {
    question: done.has("interpret") ? "done" : "active",
    data: refusedEarly && done.has("assess") ? "stopped" : done.has("assess") ? "done" : done.has("interpret") ? "active" : "",
    plan: done.has("generate") ? "done" : ["plan", "generate"].includes(next) ? "active" : "",
    execute: done.has("execute") && next !== "execute" ? "done" : next === "execute" ? "active" : "",
    verify: s.final?.status === "verified" ? "done" : verifyFailed ? "stopped" : next === "verify" ? "active" : "",
  };
  ol.replaceChildren(...Object.entries(state).map(([name, st]) =>
    h("li", { class: "timeline__step", "data-state": st || null, "aria-current": st === "active" ? "step" : null },
      name, h("span", { class: "sr-only" }, st === "done" ? " complete" : st === "active" ? " in progress" : st === "stopped" ? " stopped" : " pending"))));
}

/* ---------------------------------------------------------------- right: transitioning stage */
function renderStage(s, ph, id) {
  if (ph === "verified") return verifiedStage(s, id);
  if (ph === "refused") return refusedStage(s);
  const next = nextStage(s);
  const at = s.attempts.length;
  const stage = h("section", { class: "stage", "aria-label": "Analysis state" });
  if (ph === "repairing") {
    const f = failedAttempt(s);
    stage.append(
      h("div", { class: "stage__label" }, h("span", { class: "verdict verdict--failed" }, "Verification failed")),
      h("p", { class: "lead" }, "The generated proof did not pass verification. No result is shown from it."),
      comparison(f.verification, s.claimed_value),
      f.failure_reason && h("p", { class: "meta" }, f.failure_reason),
      h("div", { class: "stage__label", style: { "margin-top": "var(--space-7)" } }, marker("Repairing analysis", "active"),
        h("span", { class: "meta" }, `attempt ${at + (next === "generate" ? 1 : 0)}`)));
  } else {
    stage.append(
      h("div", { class: "stage__label" }, marker(ph === "analyzing" ? "Analyzing" : ph === "executing" ? "Executing" : "Verifying", "active")),
      h("h2", { class: "stage__headline" }, HEADLINE[next] || "Analyzing"));
  }
  const idx = ORDER.indexOf(next);
  stage.append(
    h("ol", { class: "worklist" }, ORDER.map((st, i) => {
      const state = i < idx ? "complete" : i === idx ? "progress" : "pending";
      return h("li", { "data-state": state }, h("span", {}, WORK[st]),
        state === "complete" ? marker("Complete", "pass") : state === "progress" ? marker("In progress", "active") : h("span", { class: "meta" }, "—"));
    })),
    h("div", { class: "activity-line", "aria-hidden": "true" }));
  return stage;
}

function comparison(v, claimed) {
  const cells = [];
  const exec = v?.executed_value, ind = v?.independent_value;
  const same = JSON.stringify(exec) === JSON.stringify(ind);
  if (claimed != null) cells.push(["Claimed", fmtVal(claimed), ""]);
  cells.push(["Executed", fmtVal(exec), ""]);
  cells.push(["Independent", fmtVal(ind), ind == null ? "" : same ? "is-match" : "is-mismatch"]);
  cells.push(["Match", v?.match ? "Yes" : "No", v?.match ? "is-match" : "is-mismatch"]);
  return h("div", { class: "compare" }, cells.map(([k, val, cls]) =>
    h("div", {}, h("p", { class: "eyebrow" }, k), h("p", { class: `compare__value ${cls}` }, val))));
}

function verifiedStage(s, id) {
  const f = s.final;
  const v = f.verification;
  const isScalar = s.plan.output === "scalar";
  const display = f.display || [];
  const ranking = s.plan.output === "ranking";
  const single = ranking && display.length === 1;  // "which region has the highest revenue": one answer
  const top = !isScalar && display.length
    ? (ranking ? display[0] : display.reduce((a, b) => (Number(b.value) > Number(a.value) ? b : a), display[0])) : null;
  const numberEl = h("p", { class: "result-number result__number", "aria-label": isScalar ? f.answer : top?.formatted });
  const showList = !isScalar && !single;
  const stage = h("section", { class: "stage result", "aria-label": "Verified result" },
    h("div", { class: "stage__label" }, h("span", { class: "verdict verdict--verified" }, "Verified result")),
    h("p", { class: "result__metric" }, describeMetric(s)),
    single && h("p", { class: "result__key" }, top.key),
    numberEl,
    showList && top && h("p", { class: "meta", style: { "margin-top": "calc(-1 * var(--space-3))", "margin-bottom": "var(--space-5)" } },
      ranking ? `Rank 1: ${top.key}` : `Highest: ${top.key}`),
    showList && barChart(display, { columns: Boolean(s.spec?.time_grain), title: describeMetric(s) }),
    showList && table([s.spec?.group_by ? label(s.spec.group_by.split(".").pop()) : s.spec?.time_grain || "key", "value"],
      display.map((d) => [d.key, d.formatted]), { numeric: ["value"], caption: "Verified values" }),
    h("div", { class: "result__statement" },
      h("span", { class: "verdict verdict--verified" }, "Verified"),
      h("p", {}, "The proof ran in an isolated sandbox, reproduced the same result in a fresh run, and an independent DuckDB computation matched it exactly.")),
    comparison(v, s.claimed_value),
    h("p", { class: "meta result__evidence-line", style: { "margin-top": "var(--space-4)" } },
      "Verified against ", s.datasets.map((d) => `${d.name} (${fmtInt(d.rows)} rows)`).join(", "), "."),
    h("div", { class: "btn-row", style: { "margin-top": "var(--space-6)" } },
      h("a", { class: "btn btn--primary", href: "#proof", onclick: (e) => { e.preventDefault(); openProof(); } }, "View proof ", arrow()),
      h("a", { class: "btn btn--secondary", href: `#/workbench/${id}` }, "Open in workbench ", arrow()),
      h("button", { class: "btn btn--secondary", type: "button", onclick: () => diagnostics(s) }, "Diagnostics ", arrow()),
      askAgain(s),
      h("a", { class: "btn btn--secondary", href: api.exportUrl(id), download: `analysis-${id}.json` }, "Export JSON")));
  const shown = isScalar ? f.answer : top?.formatted;
  if (shown) {  // "815,497.70 USD" -> large number, smaller unit
    const m = /^(\S+)\s+(\S+)$/.exec(shown);
    const value = h("span", {}, m ? m[1] : shown);
    numberEl.append(value);
    if (m) numberEl.append(h("span", { class: "result__unit" }, m[2]));
    animateNumber(value, m ? m[1] : shown);
  }
  return enter(stage);
}

function openProof() {
  const d = document.getElementById("proof");
  if (!d) return;
  d.open = true;
  d.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  d.querySelector("summary")?.focus({ preventScroll: true });
}

function refusedStage(s) {
  const f = s.final;
  const failed = failedAttempt(s);
  const clarify = f.suggestions?.length > 0;
  return h("section", { class: "stage", "aria-label": "Refusal" },
    h("div", { class: "stage__label" },
      h("span", { class: "verdict verdict--refused" }, clarify ? "Needs a more specific question" : "Cannot determine"),
      marker(STATE_LABEL[f.answerability] || f.answerability, "neutral")),
    h("h2", { class: "stage__headline" }, f.answer),
    h("p", { class: "refusal__reason" }, f.reason),
    clarify && suggestions(f.suggestions),
    f.hint && h("p", { class: "meta", style: { "margin-top": "var(--space-4)" } }, f.hint),
    (f.also?.length || failed) && h("div", { class: "refusal__why" },
      h("p", { class: "eyebrow" }, "Why"),
      f.also?.map((r) => h("p", {}, r)),
      failed && comparison(failed.verification, s.claimed_value)),
    f.fixes?.length > 0 && unblockers(f.fixes),
    h("p", { class: "refusal__end" }, "No verified result produced."),
    h("div", { class: "btn-row", style: { "margin-top": "var(--space-6)" } },
      h("a", { class: "btn btn--primary", href: "#/" }, "Ask another question ", arrow()),
      askAgain(s),
      h("button", { class: "btn btn--secondary", type: "button", onclick: () => diagnostics(s) }, "Diagnostics ", arrow())));
}

/** Data fixes that, tried on a copy of the data, would make the question answerable or clear a reason. */
function unblockers(suggestions) {
  const byFix = new Map();
  suggestions.forEach((x) => byFix.set(x.fix.id, [...(byFix.get(x.fix.id) || []), x]));
  return h("div", { class: "refusal__why", style: { "margin-top": "var(--space-5)" } },
    h("p", { class: "eyebrow" }, "What would make this answerable"),
    h("p", { class: "meta" }, "Each fix was tried on a copy of the data. Nothing has been changed; you preview a fix before applying it, then run the question again."),
    [...byFix.values()].map((xs) => {
      const opt = xs[0].fix;
      const pick = xs.find((x) => x.answerable) || xs[0];
      return h("div", { style: { "margin-top": "var(--space-4)" } },
        h("p", {}, h("strong", {}, opt.title), " ", h("span", { class: "meta" }, `· ${opt.table}${opt.column ? "." + opt.column : ""}`)),
        xs.map((x) => h("p", { class: "meta" }, `${x.choice_label ? x.choice_label + ": " : ""}`
          + (x.answerable ? "the question becomes answerable." : `clears one reason; still blocked by: ${x.remaining}`))),
        h("button", { class: "btn btn--secondary story__fix", type: "button", onclick: () => openFixDrawer(opt, pick.choice) },
          "Review this fix ", arrow()));
    }));
}

/** Rewritten questions the data can answer; each was already checked and runs a normal verified analysis. */
function suggestions(list) {
  return h("div", { class: "refusal__why", style: { "margin-top": "var(--space-5)" } },
    h("p", { class: "eyebrow" }, "Did you mean"),
    h("p", { class: "meta" }, "Each of these was checked against the data and can be answered with a verified proof. Nothing is assumed until you pick one."),
    list.map((x) => {
      const btn = h("button", { class: "btn btn--secondary", type: "button", style: { "margin-top": "var(--space-3)", "text-align": "left" } },
        x.question, " ", arrow());
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          const { id } = await api.analyze(x.question, null);
          update({ pending: { id, question: x.question } });
          location.hash = `#/analysis/${id}`;
        } catch (e) {
          btn.disabled = false;
          toast(e instanceof ConnectionError ? "Connection lost" : e.message, "error");
        }
      });
      return h("div", {}, btn, h("p", { class: "meta" }, x.why));
    }));
}

/** Start a fresh analysis of the same question (and claim) against the current data. */
function askAgain(s) {
  const btn = h("button", { class: "btn btn--secondary", type: "button" }, "Run again as new analysis");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const { id } = await api.analyze(s.question, s.claimed_value ?? null);
      update({ pending: { id, question: s.question } });
      location.hash = `#/analysis/${id}`;
    } catch (e) {
      btn.disabled = false;
      toast(e instanceof ConnectionError ? "Connection lost" : e.message, "error");
    }
  });
  return btn;
}

/* ---------------------------------------------------------------- below the split: evidence, execution, proof */
function renderAfter(host, s, id) {
  if (host.dataset.rendered) return;
  host.dataset.rendered = "1";
  if (s.final.status !== "verified") return;
  const f = s.final;
  const at = lastAttempt(s);
  const ex = at.execution;
  const execFacts = h("div");
  const setExecFacts = (e, extra) => execFacts.replaceChildren(facts([
    ["Status", e.status === "ok" ? "Success" : e.status],
    ["Duration", `${e.duration_s.toFixed(2)} s`],
    ["Exit code", String(e.exit_code)],
    ["Sandbox", store.status?.sandbox?.provider || "—"],
    ["Attempts", String(s.attempts.length)],
    extra,
  ]));
  setExecFacts(ex);
  const output = h("pre", { class: "output", tabindex: "0" }, ex.stdout.trim() || "(no output)");

  const rerunBtn = h("button", { class: "btn btn--tech", type: "button" }, "Re-run ", arrow());
  rerunBtn.addEventListener("click", async () => {
    rerunBtn.disabled = true;
    rerunBtn.textContent = "Executing…";
    try {
      const r = await api.verifyCode(id);
      const ok = r.verification.status === "verified" && r.matches_shown_result;
      rerunBtn.textContent = ok ? "Verified" : "Failed";
      setExecFacts(r.execution, ["Re-run", ok ? "Verified, identical result" : `Failed: ${r.verification.checks.filter((c) => !c.passed).map((c) => c.name).join(", ")}`]);
      output.textContent = (r.execution.stdout || "").trim() || r.execution.error || "(no output)";
      toast(ok ? "Re-run verified" : "Re-run failed verification", ok ? "info" : "error");
    } catch (e) {
      rerunBtn.textContent = "Unavailable";
      setExecFacts(ex, ["Re-run", e instanceof ConnectionError ? "Connection lost" : e.message]);
      toast(e instanceof ConnectionError ? "Connection lost" : "Re-run not possible", "error");
    }
    setTimeout(() => { rerunBtn.disabled = false; rerunBtn.replaceChildren("Re-run ", arrow()); }, 2400);
  });
  const copyBtn = h("button", { class: "btn btn--tech", type: "button" }, "Copy");
  copyBtn.addEventListener("click", async () => {
    const ok = await copyText(f.proof_code);
    copyBtn.textContent = ok ? "Copied" : "Copy failed";
    if (ok) toast("Proof copied");
    setTimeout(() => { copyBtn.textContent = "Copy"; }, 1600);
  });
  const dlBtn = h("a", { class: "btn btn--tech", href: api.bundleUrl(id), title: "proof.py with its data, requirements and a run script" }, "Download bundle");
  const wbBtn = h("a", { class: "btn btn--tech", href: `#/workbench/${id}` }, "Open in workbench ", arrow());
  const lines = f.proof_code.trimEnd().split("\n").length;

  host.append(
    reveal(h("section", { class: "section" },
      h("div", { class: "section__head" }, h("h2", { class: "eyebrow" }, "Evidence")),
      h("div", { class: "two-col" },
        h("div", {}, h("p", { class: "eyebrow", style: { margin: "var(--space-4) 0 var(--space-3)" } }, "Datasets used"),
          h("ul", { class: "datasets-used" }, s.datasets.map((d) => h("li", {}, h("span", {}, d.name), h("span", { class: "num meta" }, `${fmtInt(d.rows)} rows`))))),
        h("div", {}, h("p", { class: "eyebrow", style: { margin: "var(--space-4) 0 var(--space-3)" } }, "Checks performed"),
          h("ul", { class: "checks" }, f.verification.checks.map((c) => h("li", {}, h("span", {}, label(c.name)), marker(c.passed ? "Pass" : "Fail", c.passed ? "pass" : "fail")))))))),
    reveal(h("section", { class: "section" },
      h("div", { class: "section__head" }, h("h2", { class: "eyebrow" }, "Execution")),
      h("div", { class: "tech" },
        h("div", { class: "tech__body" }, execFacts),
        h("div", { class: "tech__head" }, h("p", { class: "eyebrow" }, "Output")),
        output,
        ex.stderr?.trim() && h("pre", { class: "output output--err" }, ex.stderr.trim())))),
    reveal(h("section", { class: "section", id: "proof-section" },
      h("details", { class: "disclosure", id: "proof" },
        h("summary", {}, h("span", {}, "Executable proof ", h("span", { class: "meta", style: { "letter-spacing": "0", "text-transform": "none", "font-weight": "400" } }, `Python · ${lines} lines`)),
          h("span", { class: "row__go" }, h("span", { class: "row__go-label" }, "View code"), arrow())),
        h("div", { class: "tech" },
          h("div", { class: "tech__head" }, h("p", { class: "eyebrow" }, "proof.py"), h("div", { class: "btn-row" }, copyBtn, dlBtn, wbBtn, rerunBtn)),
          codeBlock(f.proof_code))))));
}

/* ---------------------------------------------------------------- diagnostics drawer */
function diagnostics(s) {
  const a = s.answerability;
  openDrawer("Diagnostics", [
    h("section", {}, h("p", { class: "eyebrow" }, "Interpretation"),
      facts([["Interpreter", s.interpreter || "—"], ["Metric", describeMetric(s) || "—"]]),
      s.spec && h("div", { class: "tech", style: { "margin-top": "var(--space-3)" } },
        h("pre", { class: "output" }, JSON.stringify(s.spec, null, 2)))),
    a && h("section", {}, h("p", { class: "eyebrow" }, "Answerability"),
      h("p", {}, marker(STATE_LABEL[a.status] || a.status, a.status === "ANSWERABLE" ? "pass" : "fail")),
      h("ul", { class: "notes" }, [...a.reasons, ...a.diagnostics].map((d) => h("li", {}, d)))),
    s.issues.length > 0 && h("section", {}, h("p", { class: "eyebrow" }, "Data issues in the tables used"),
      table(["table", "column", "issue", "detail"], s.issues.map((i) => [i.table, i.column, label(i.kind), i.detail]))),
    s.attempts.map((at) => h("section", {},
      h("p", { class: "eyebrow" }, `Attempt ${pad(at.number)} · ${at.source}`),
      facts([
        ["Execution", at.execution ? `${at.execution.status}${at.execution.error ? `: ${at.execution.error}` : ""}` : "—"],
        at.execution && ["Duration", `${at.execution.duration_s.toFixed(2)} s`],
        ["Verification", at.verification ? at.verification.status : "—"],
        at.failure_reason && ["Failure", at.failure_reason],
      ]),
      at.execution?.stderr?.trim() && h("div", { class: "tech", style: { "margin-top": "var(--space-3)" } },
        h("pre", { class: "output output--err" }, at.execution.stderr.trim())),
      h("details", { class: "disclosure", style: { "margin-top": "var(--space-3)" } },
        h("summary", {}, "Code", arrow()), h("div", { class: "tech" }, codeBlock(at.code))))),
    h("section", {}, h("p", { class: "eyebrow" }, "Stage log"),
      table(["stage", "next", "duration (s)"], s.stages.filter((e) => e.stage !== "complete").map((e) => [e.stage, e.next, e.duration_s]),
        { numeric: ["duration (s)"], mono: ["stage", "next"] })),
  ]);
}
