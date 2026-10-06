// System: real configuration, sandbox health, security controls, verification checks, benchmark.
import { ConnectionError, api } from "../api.js";
import { facts, marker, table, toast } from "../components.js";
import { arrow, h, label, setTitle } from "../dom.js";
import { reveal } from "../motion.js";
import { store, update } from "../state.js";

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
  const bench = h("div");
  main.replaceChildren(h("div", { class: "view" },
    h("header", { class: "view__head" },
      h("p", { class: "eyebrow" }, "System"),
      h("h1", { class: "title", tabindex: "-1" }, s.sandbox.ready ? "Sandbox ready." : "Sandbox unavailable."),
      !s.sandbox.ready && h("p", { class: "lead" }, `The execution environment did not complete a health check (${s.sandbox.status}${s.sandbox.error ? `: ${s.sandbox.error}` : ""}). Analyses will be refused until it does.`)),
    h("section", {},
      h("div", { class: "section__head" }, h("h2", { class: "eyebrow" }, "Configuration")),
      facts([
        ["Interpreter", s.interpreter],
        ["Sandbox", `${s.sandbox.provider} · ${s.sandbox.ready ? "ready" : "not ready"}`],
        ["Timeout", `${s.sandbox.timeout_s} s per execution`],
        ["Memory cap", `${s.sandbox.memory_mb} MB`],
        ["Repair attempts", String(s.max_repairs)],
        ["Workspace", `${s.workspace} · ${s.tables} tables`],
        ["Uploads", s.supported_uploads.join(", ")],
      ])),
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
}

async function renderBench(host, s) {
  let report = null;
  try { report = await api.benchmark(); } catch { /* shown as not run */ }
  const btn = s.benchmark_available && h("button", { class: "btn btn--secondary", type: "button" }, "Run benchmark ", arrow());
  btn?.addEventListener("click", async () => {
    btn.disabled = true;
    btn.replaceChildren("Running every labelled question…");
    update({ activity: { busy: true, label: "Benchmarking" } });
    try {
      await api.runBenchmark();
      toast("Benchmark complete");
    } catch (e) {
      toast(e instanceof ConnectionError ? "Connection lost" : e.message, "error");
    }
    update({ activity: null });
    renderBench(host, s);
  });
  const body = [];
  if (!s.benchmark_available) body.push(h("p", { class: "meta" }, "The benchmark runs on the demonstration workspace, where ground truth is known."));
  if (report) {
    body.push(h("p", { class: "meta", style: { "margin-bottom": "var(--space-3)" } },
      `Last run ${new Date(report.run_at).toLocaleString()} · ${report.cases} questions · ${report.duration_s} s`));
    body.push(h("ul", { class: "metric-list" }, Object.entries(report.metrics).map(([k, v]) =>
      h("li", {}, h("span", {}, label(k)), h("span", { class: "num" }, v == null ? "n/a" : `${(v * 100).toFixed(1)}%`)))));
    body.push(h("details", { class: "disclosure", style: { "margin-top": "var(--space-5)" } },
      h("summary", {}, "Per-question results", arrow()),
      table(["question", "expected", "outcome", "correct"], report.rows.map((r) =>
        [r.question, r.answerable ? "answer" : "refusal", r.status, r.correct ? "yes" : "no"]))));
  } else {
    body.push(h("p", { class: "meta" }, "No benchmark has been run yet. Metrics appear here only after a real run."));
  }
  host.replaceChildren(...body, btn && h("div", { style: { "margin-top": "var(--space-5)" } }, btn));
}
