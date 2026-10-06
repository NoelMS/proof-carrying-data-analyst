// Data quality: each detected issue explained as evidence, with how the analysis handles it.
import { marker } from "../components.js";
import { h, pad, setTitle } from "../dom.js";
import { reveal } from "../motion.js";
import { store } from "../state.js";

// Describes behaviour implemented in app/answerability.py and app/traps.py.
const STORY = {
  duplicate_rows: ["Duplicate records", "handled", "Counting identical records twice would overstate totals and counts.",
    "Exact duplicates (identical in every field, including the identifier) are counted once; the plan states how many were removed."],
  conflicting_records: ["Conflicting records", "blocks", "Two versions of the same entity disagree, so any figure that depends on it could be either value.",
    "Questions that use this table are refused rather than choosing one version."],
  mixed_currency: ["Mixed currencies", "conditional", "Adding amounts in different currencies produces a meaningless total.",
    "Amounts are combined only after converting to a currency you request, using an exchange-rate table in the data. Otherwise the question is refused."],
  currency_symbols: ["Currency symbols in values", "blocks", "Values such as “$5.00” are text, not numbers.",
    "The column is not used as a numeric measure."],
  mixed_units: ["Mixed units", "blocks", "Summing kilograms and pounds gives a number with no unit.",
    "Aggregations over the affected measure are refused; no conversion is assumed."],
  ambiguous_date: ["Ambiguous dates", "blocks", "03/04/2024 could be 3 April or 4 March; periods and totals would change with the reading.",
    "Questions that filter or group by this date are refused."],
  inconsistent_date_format: ["Inconsistent dates", "blocks", "Dates in several formats cannot be read reliably.",
    "Questions that filter or group by this date are refused."],
  timezone_unspecified: ["No timezone", "surfaced", "Timestamps near midnight can fall on different days in different zones.",
    "Dates are taken as recorded, and the analysis says so."],
  missing_values: ["Missing values", "conditional", "Treating a blank as zero or dropping it silently changes the result.",
    "A question is refused when the column it measures, groups by, filters on or counts has missing values."],
  negative_values: ["Negative values", "surfaced", "Negative quantities or amounts may be returns or errors.",
    "Reported as a diagnostic on affected analyses; rows are not dropped."],
  orphan_keys: ["Unmatched keys", "blocks", "Records that reference a missing parent would be lost or mislabelled in a join.",
    "Joins through this relationship are refused."],
  temporal_contradiction: ["Out-of-order dates", "surfaced", "A payment or shipment dated before its order suggests a data error.",
    "Reported as a diagnostic; rows are not dropped or reordered."],
  prompt_injection: ["Instruction-like text", "handled", "Text in a cell could try to steer an AI model.",
    "Treated strictly as data: withheld from model context and never executed."],
  derived_table: ["Pre-aggregated table", "info", "Summary tables can disagree with the records they claim to summarize.",
    "Not used as a source of truth for record-level metrics."],
};
const STATUS = { handled: "Handled", blocks: "Blocks affected questions", conditional: "Depends on question", surfaced: "Surfaced", info: "Informational" };

export function issueStory(issue) {
  const [name, status, why, action] = STORY[issue.kind] || [issue.kind, "info", "", ""];
  return h("article", { class: "story" },
    h("div", {},
      h("div", { class: "story__title" }, h("h3", { class: "story__name" }, name)),
      h("p", { class: "story__where" }, issue.column ? `${issue.table}.${issue.column}` : issue.table),
      h("p", { class: "story__detail" }, issue.detail),
      h("p", { style: { "margin-top": "var(--space-3)" } }, marker(STATUS[status], status))),
    h("dl", { class: "story__explain" },
      why && [h("dt", {}, "Why this matters"), h("dd", {}, why)],
      action && [h("dt", {}, "How the analysis handles it"), h("dd", {}, action)]));
}

export function renderQuality(main) {
  setTitle("Data quality");
  const ds = store.datasets;
  if (!ds) { main.replaceChildren(h("div", { class: "view" }, h("p", { class: "eyebrow" }, "Loading data quality"))); return; }
  const order = Object.keys(STORY);
  const issues = [...ds.issues].sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind));
  const blocking = issues.filter((i) => STORY[i.kind]?.[1] === "blocks").length;
  main.replaceChildren(h("div", { class: "view" },
    h("header", { class: "view__head" },
      h("p", { class: "eyebrow" }, "Data quality"),
      h("h1", { class: "title", tabindex: "-1" }, `${pad(issues.length)} issues detected.`),
      h("p", { class: "lead" }, issues.length
        ? `${blocking} can block questions that depend on them. Issues are facts about the data; whether one matters depends on the tables and columns a question uses.`
        : "No issues were detected in the current workspace.")),
    h("div", { class: "rows" }, issues.map((i, n) => reveal(issueStory(i), Math.min(n, 6))))));
}
