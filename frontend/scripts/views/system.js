// System: real configuration, sandbox health, security controls, verification checks, benchmark.
import { ConnectionError, api } from "../api.js";
import { facts, marker, table, toast } from "../components.js";
import { arrow, h, label, setTitle } from "../dom.js";
import { reveal } from "../motion.js";
import { store, update } from "../state.js";
import { renderAgent } from "./agent.js";
import { STATE_LABEL } from "./analysis.js";

const CONTROLS = [
  ["Static policy", "Import allowlist; no eval, exec, open, dynamic attribute access, file writers or alternative readers."],
  ["Ephemeral workspace", "Fresh directory per run holding read-only copies of only the tables in the plan; deleted afterwards."],
  ["Isolated interpreter", "Python in isolated mode with an empty environment: no credentials are present to read."],
  ["Runtime guard", "Audit hook blocks process creation, sockets, ctypes, file writes and reads outside the workspace."],
  ["Resource limits", "Wall-clock timeout, memory cap and single-process limit; output capped at 64 KB."],
  ["Docker provider", "Optional: no network, read-only filesystem, dropped capabilities, non-root user, pids limit."],
];
const CHECKS = [
  ["execution", "Exit code 0, no timeout or violation, exactly one RESULT line"],
  ["output contract", "Scalar, mapping or ranking shape matches the plan"],
  ["precision", "Exactly the decimal places the source data justifies"],
  ["static policy", "Code passes the static policy"],
  ["data access", "Code reads every table the plan requires"],
  ["no exception suppression", "No try/except that could hide a failure"],
  ["no hardcoded result", "The reported value is not a literal in the code"],
  ["reproduction", "Identical result in a fresh sandbox"],
  ["independent recomputation", "DuckDB SQL computed from the plan matches exactly"],
  ["claim matches execution", "When a value is claimed, it equals the executed value"],
];

export function renderSystem(main) {
  setTitle("System");
  const s = store.status;
  if (!s) { main.replaceChildren(h("div", { class: "view" }, h("p", { class: "eyebrow" }, "Loading system status"))); return; }
  const bench = h("div"), agent = h("div");
  main.replaceChildren(h("div", { class: "view" },
    h("header", { class: "view__head" },
      h("p", { class: "eyebrow" }, "System"),
      h("h1", { class: "title", tabindex: "-1" }, s.sandbox.ready ? "Sandbox ready." : "Sandbox unavailable."),
      !s.sandbox.ready && h("p", { class: "lead" }, `The execution environment did not complete a health check (${s.sandbox.status}${s.sandbox.error ? `: ${s.sandbox.error}` : ""}). Analyses will be refused until it does.`)),
    h("section", {},
      h("div", { class: "section__head" }, h("h2", { class: "eyebrow" }, "Configuration")),
      facts([
        ["Agent", s.interpreter],
        ["Sandbox", `${s.sandbox.provider} · ${s.sandbox.ready ? "ready" : "not ready"}`],
        ["Timeout", `${s.sandbox.timeout_s} s per execution`],
        ["Memory cap", `${s.sandbox.memory_mb} MB`],
        ["Repair attempts", String(s.max_repairs)],
        ["Workspace", `${s.workspace} · ${s.tables} tables`],
        ["Uploads", s.supported_uploads.join(", ")],
      ])),
    h("section", { class: "section", id: "agent" },
      h("div", { class: "section__head" }, h("h2", { class: "eyebrow" }, "How questions are answered"),
        h("span", { class: "meta" }, "Who reads questions and writes the proofs")),
      agent),
    h("section", { class: "section", id: "security" },
      h("div", { class: "section__head" }, h("h2", { class: "eyebrow" }, "Execution security"), h("span", { class: "meta" }, "Applied to every run")),
      h("div", { class: "rows" }, CONTROLS.map(([k, v], i) => reveal(h("div", { class: "row row--compact" },
        h("span", { class: "row__index" }, String(i + 1).padStart(2, "0")), h("span", { class: "row__title" }, k, h("span", { class: "row__sub" }, v)),
        k === "Docker provider" ? marker(s.sandbox.provider === "docker" ? "Active" : "Available", s.sandbox.provider === "docker" ? "pass" : "neutral") : marker("Active", "pass")), i)))),
    h("section", { class: "section", id: "verification" },
      h("div", { class: "section__head" }, h("h2", { class: "eyebrow" }, "Verification checks"), h("span", { class: "meta" }, "All must pass")),
      table(["check", "requirement"], CHECKS)),
    h("section", { class: "section" },
      h("div", { class: "section__head" }, h("h2", { class: "eyebrow" }, "Benchmark"), h("span", { class: "meta" }, "Labelled questions with independently computed answers")),
      bench)));
  renderBench(bench, s);
  renderAgent(agent);
}

const BENCH_STAGES = ["interpret", "assess", "plan", "generate", "execute", "verify", "reproduce"];
const STAGE_TEXT = { interpret: "Read", assess: "Check data", plan: "Plan", generate: "Write proof", execute: "Execute",
                     verify: "Verify", reproduce: "Reproduce", answer: "Answer", complete: "Done" };
let benchTimer = null;

async function renderBench(host, s) {
  clearTimeout(benchTimer);
  let report = null, prog = null;
  try { [report, prog] = await Promise.all([api.benchmark(), api.benchmarkProgress()]); } catch { /* shown as not run */ }
  if (!host.isConnected) return;
  if (prog?.running) return watchBench(host, s);

  const btn = s.benchmark_available ? h("button", { class: "btn btn--primary", type: "button" }, "Run benchmark ", arrow()) : null;
  btn?.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      await api.runBenchmark();
      watchBench(host, s);
    } catch (e) {
      toast(e instanceof ConnectionError ? "Connection lost" : e.message, "error");
      btn.disabled = false;
    }
  });
  const body = [h("p", { class: "meta", style: { "margin-bottom": "var(--space-4)" } },
    "Runs every labelled demonstration question through the full workflow, on the original demonstration data ",
    "(applied fixes are not used, because the expected answers refer to the original records).")];
  if (prog?.error) body.push(h("p", { class: "notice-inline", style: { "margin-bottom": "var(--space-4)" } }, `The last run stopped: ${prog.error}`));
  if (prog?.cancelled) {
    body.push(h("div", { style: { "margin-bottom": "var(--space-6)" } },
      h("p", { class: "notice-inline" }, `The last run was cancelled after ${prog.rows.length} of ${prog.total} questions. `
        + "Cancelled runs are not saved; the metrics below are from the last complete run."),
      h("details", { class: "disclosure", style: { "margin-top": "var(--space-3)" } },
        h("summary", {}, `Results of the cancelled run (${prog.rows.length})`, arrow()),
        benchRows(prog.rows, prog.rows.length, null))));
  }
  if (report) {
    body.push(h("p", { class: "meta", style: { "margin-bottom": "var(--space-3)" } },
      `Last run ${new Date(report.run_at).toLocaleString()} · ${report.cases} questions · ${report.duration_s} s`));
    body.push(h("ul", { class: "metric-list" }, Object.entries(report.metrics).map(([k, v]) =>
      h("li", {}, h("span", {}, label(k)), h("span", { class: "num" }, v == null ? "n/a" : `${(v * 100).toFixed(1)}%`)))));
    const pct = (v) => (v == null ? "n/a" : `${(v * 100).toFixed(1)}%`);
    if (report.by_dataset) body.push(h("div", { style: { "margin-top": "var(--space-4)" } }, table(
      ["dataset", "questions", "answers right", "refusals right", "confident wrong"],
      Object.entries(report.by_dataset).map(([d, m]) =>
        [d, String(m.questions), pct(m.valid_answer_accuracy), pct(m.refusal_accuracy), pct(m.confident_wrong_rate)]),
      { numeric: ["questions", "answers right", "refusals right", "confident wrong"] })));
    body.push(h("details", { class: "disclosure", style: { "margin-top": "var(--space-5)" } },
      h("summary", {}, "Per-question results", arrow()), benchRows(report.rows, report.rows.length, null)));
  } else {
    body.push(h("p", { class: "meta" }, "No benchmark has been run yet. Metrics appear here only after a real run."));
  }
  host.replaceChildren(...body, btn && h("div", { style: { "margin-top": "var(--space-5)" } }, btn));
}

/** Poll the real progress of a running benchmark and render it until it finishes. */
function watchBench(host, s) {
  update({ activity: { busy: true, label: "Benchmarking" } });
  const tick = async () => {
    if (!host.isConnected) { update({ activity: null }); return; }
    let p;
    try { p = await api.benchmarkProgress(); } catch (e) {
      host.replaceChildren(h("p", { class: "notice-inline" }, e instanceof ConnectionError ? "Connection lost while the benchmark was running." : e.message));
      update({ activity: null });
      return;
    }
    if (!p.running) {
      update({ activity: null });
      toast(p.error ? "Benchmark stopped" : p.cancelled ? "Benchmark cancelled" : "Benchmark complete", p.error || p.cancelled ? "error" : "info");
      return renderBench(host, s);
    }
    host.replaceChildren(benchProgress(p));
    benchTimer = setTimeout(tick, 450);
  };
  tick();
}

function cancelButton(p) {
  const btn = h("button", { class: "btn btn--secondary", type: "button", disabled: p.cancel || null },
    p.cancel ? "Cancelling after the current question…" : "Cancel benchmark");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.replaceChildren("Cancelling after the current question…");
    try { await api.cancelBenchmark(); } catch (e) { toast(e.message, "error"); }
  });
  return btn;
}

function benchProgress(p) {
  const rows = p.rows;
  const elapsed = (Date.now() / 1000 - p.started_at).toFixed(1);
  const answerable = rows.filter((r) => r.answerable), unanswerable = rows.filter((r) => !r.answerable);
  const avg = rows.length ? rows.reduce((a, r) => a + r.duration_s, 0) / rows.length : null;
  const remaining = avg != null ? Math.max(0, (p.total - rows.length) * avg) : null;
  const c = p.current;
  return h("div", { "aria-live": "polite" },
    h("div", { class: "btn-row", style: { "justify-content": "space-between", "margin-bottom": "var(--space-2)" } },
      h("p", { class: "eyebrow" }, `${p.cancel ? "Cancelling" : "Running"} · ${rows.length} of ${p.total} complete · ${elapsed} s elapsed`
        + (remaining != null && !p.cancel ? ` · about ${Math.ceil(remaining)} s left at the current pace` : "")),
      cancelButton(p)),
    h("div", { class: "progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(p.total), "aria-valuenow": String(rows.length),
      "aria-label": "Questions completed" }, h("div", { class: "progress__bar", style: { "--p": (rows.length / p.total).toFixed(4) } })),
    c && h("div", { class: "bench-now" },
      h("p", { class: "eyebrow" }, `Question ${c.index + 1} of ${p.total} · expected ${c.answerable ? "a verified answer" : "a refusal"}`),
      h("p", { class: "bench-now__q" }, c.question),
      h("div", { class: "stage-pills", "aria-label": "Workflow stage" }, BENCH_STAGES.map((st) => {
        const done = c.stages_done.includes(st);
        const active = !done && c.stage === st;
        return h("span", { class: "stage-pill", "data-state": done ? "done" : active ? "active" : null }, STAGE_TEXT[st]);
      }))),
    h("div", { class: "tally" },
      tally(`${answerable.filter((r) => r.correct).length}/${answerable.length}`, "correct answers"),
      tally(`${unanswerable.filter((r) => r.correct).length}/${unanswerable.length}`, "correct refusals"),
      tally(String(rows.filter((r) => r.confident_wrong).length), "confident-wrong"),
      tally(String(rows.filter((r) => r.reproduced).length), "proofs reproduced")),
    benchRows(rows, p.total, c?.index ?? null, p.cases));
}

const tally = (v, l) => h("div", {}, h("p", { class: "tally__v" }, v), h("p", { class: "meta" }, l));

function benchRows(rows, total, currentIndex, cases) {
  // rows[i] is the result for case i; cases[i] (while running) gives the expectation before it runs
  return h("div", { role: "list", style: { "margin-top": "var(--space-4)", "border-top": "1px solid var(--color-text)" } },
    Array.from({ length: total }, (_, i) => benchRow(i, rows[i], rows[i] || cases?.[i] || {}, currentIndex)));
}

const short = (v) => {
  const t = typeof v === "string" ? v : JSON.stringify(v);
  return t.length > 140 ? t.slice(0, 139) + "…" : t;
};
const reasonLabel = (code) => STATE_LABEL[code] || (code ? code.toLowerCase().replace(/_/g, " ") : "any reason");

function benchRow(i, r, c, currentIndex) {
  const state = r ? "done" : i === currentIndex ? "running" : "pending";
  const outcome = !r ? (state === "running" ? marker("Running", "active") : h("span", { class: "meta" }, "Waiting"))
    : marker(r.status === "verified" ? "Verified" : "Refused", r.status === "verified" ? "pass" : "neutral");
  const verdict = !r ? "" : r.correct ? marker("Correct", "pass") : marker(r.confident_wrong ? "Confident-wrong" : "Wrong", "fail");
  const expected = c.answerable === undefined ? null
    : c.answerable ? `answer ${short(c.expected)}` : `refusal · ${reasonLabel(c.expected_refusal)}`;
  const got = !r ? null : r.status === "verified" ? `answer ${short(r.got)}`
    : `refusal · ${reasonLabel(r.got_refusal)} — ${r.detail}`;
  return h("div", { class: "bench-row", role: "listitem", "data-state": state },
    h("span", { class: "row__index" }, String(i + 1).padStart(2, "0")),
    h("span", {}, c.question || "",
      expected && h("span", { class: "detail" }, h("strong", {}, "Expected "), expected),
      got && h("span", { class: "detail" }, h("strong", {}, "Got "), got),
      r && h("span", { class: "detail" }, r.attempts === 0 ? "Refused before any code ran"
        : `${r.attempts} attempt${r.attempts === 1 ? "" : "s"}` + (r.reproduced ? " · proof reproduced" : ""))),
    outcome, verdict,
    h("span", { class: "meta num", style: { "text-align": "right" } }, r ? `${r.duration_s.toFixed(2)} s` : ""));
}
