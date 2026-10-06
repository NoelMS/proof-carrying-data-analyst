// Analytical workspace landing: the question surface and the data it can draw on.
import { ConnectionError, api } from "../api.js";
import { connectionLost } from "../components.js";
import { arrow, fmtInt, h, pad, setTitle } from "../dom.js";
import { reveal } from "../motion.js";
import { store, update } from "../state.js";

const isMac = /Mac|iPhone|iPad/.test(navigator.platform);

export function renderHome(main) {
  setTitle("");
  const input = h("textarea", {
    id: "question", class: "command__input", name: "question", rows: "3", required: true, maxlength: "2000",
    placeholder: "Which region generated the highest revenue in USD?", "aria-describedby": "question-hints question-error",
  });
  const claim = h("input", { id: "claim", name: "claim", type: "text", inputmode: "decimal", autocomplete: "off", placeholder: "optional" });
  const error = h("p", { class: "command__error", id: "question-error", "aria-live": "polite" });
  const submit = h("button", { class: "btn btn--primary", type: "submit" }, "Analyze question ", arrow());
  const form = h("form", { class: "command", novalidate: true },
    h("label", { class: "command__label eyebrow", for: "question" }, "Ask an analytical question"),
    input, error,
    h("div", { class: "command__foot" },
      h("div", { class: "command__hints meta", id: "question-hints" },
        h("span", {}, h("span", { class: "kbd" }, isMac ? "⌘ ↵" : "Ctrl ↵"), " analyze"),
        h("span", {}, h("span", { class: "kbd" }, "/"), " focus")),
      h("div", { class: "command__claim" },
        h("label", { class: "eyebrow", for: "claim" }, "Check a claimed value"), claim),
      submit));

  const send = async () => {
    const q = input.value.trim();
    if (!q) {
      form.setAttribute("data-invalid", "");
      error.textContent = "Enter a question to analyze.";
      input.focus();
      return;
    }
    form.removeAttribute("data-invalid");
    error.textContent = "";
    submit.disabled = true;
    submit.replaceChildren("Starting analysis…");
    try {
      const { id } = await api.analyze(q, claim.value.trim() || null);
      update({ pending: { id, question: q } });
      location.hash = `#/analysis/${id}`;
    } catch (e) {
      submit.disabled = false;
      submit.replaceChildren("Analyze question ", arrow());
      if (e instanceof ConnectionError) { update({ connection: "lost" }); main.replaceChildren(connectionLost(() => location.reload())); return; }
      form.setAttribute("data-invalid", "");
      error.textContent = e.message;
    }
  };
  form.addEventListener("submit", (e) => { e.preventDefault(); send(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); }
  });

  const dataCol = h("section", { "aria-labelledby": "available-data" });
  const exampleCol = h("section", { "aria-labelledby": "examples" });

  main.replaceChildren(h("div", { class: "view" },
    h("header", { class: "view__head" },
      h("p", { class: "eyebrow" }, "Analytical workspace"),
      h("h1", { class: "title", tabindex: "-1" }, "Ask a question. ",
        h("span", { class: "title--muted" }, "We find the evidence, execute the calculation, and verify the result."))),
    form,
    h("div", { class: "two-col section" }, dataCol, exampleCol)));

  renderData(dataCol, store.datasets);
  api.examples().then((ex) => renderExamples(exampleCol, ex, input)).catch(() => {});
  if (!matchMedia("(hover: none)").matches) input.focus({ preventScroll: true });
}

function renderData(col, ds) {
  if (!ds) { col.replaceChildren(h("p", { class: "eyebrow", id: "available-data" }, "Available data"), h("p", { class: "meta" }, "Loading datasets…")); return; }
  col.replaceChildren(
    h("div", { class: "section__head" }, h("h2", { class: "eyebrow", id: "available-data" }, "Available data"),
      h("a", { class: "link meta", href: "#/data" }, "Inspect data")),
    h("div", { class: "figures" },
      [[pad(ds.totals.tables), "datasets"], [fmtInt(ds.totals.columns), "columns"], [fmtInt(ds.totals.records), "records"]].map(([v, l], i) =>
        reveal(h("div", {}, h("p", { class: "figure__value" }, v), h("p", { class: "figure__label meta" }, l)), i))),
    h("div", { class: "rows" }, ds.tables.map((t, i) =>
      h("a", { class: "row row--compact", href: `#/data/${encodeURIComponent(t.name)}` },
        h("span", { class: "row__index" }, pad(i + 1)),
        h("span", { class: "row__title" }, t.name),
        h("span", { class: "row__go" }, h("span", { class: "row__go-label" }, `${fmtInt(t.rows)} rows`), arrow())))));
}

function renderExamples(col, examples, input) {
  if (!examples.length) return;
  col.replaceChildren(
    h("div", { class: "section__head" }, h("h2", { class: "eyebrow", id: "examples" }, "Example questions"),
      h("span", { class: "meta" }, "Answerable and unanswerable")),
    h("ul", { class: "examples" }, examples.map((q) => h("li", {},
      h("button", { type: "button", onclick: () => { input.value = q; input.focus(); } }, h("span", {}, q), arrow())))));
}

export function onDatasets(ds) {
  const col = document.querySelector("[aria-labelledby='available-data']");
  if (col) renderData(col, ds);
}
