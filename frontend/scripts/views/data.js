// Datasets: inventory, upload, and a focused inspector per table.
import { ApiError, ConnectionError, api, filesToPayload } from "../api.js";
import { connectionLost, errorNotice, facts, marker, table, toast } from "../components.js";
import { arrow, fmtInt, h, pad, setTitle } from "../dom.js";
import { reveal } from "../motion.js";
import { store, update } from "../state.js";
import { issueStory } from "./quality.js";

export function renderDataList(main) {
  setTitle("Data");
  const ds = store.datasets;
  if (!ds) { main.replaceChildren(h("div", { class: "view" }, h("p", { class: "eyebrow" }, "Loading datasets"))); return; }
  const uploads = ds.uploads || [];
  const status = h("p", { class: "meta", "aria-live": "polite", style: { "margin-top": "var(--space-3)" } });

  const act = async (message, call, done) => {
    status.textContent = message;
    try {
      const datasets = await call();
      update({ datasets, status: await api.status() });
      if (done) toast(done);
    } catch (e) {
      status.textContent = e instanceof ConnectionError ? "Connection lost." : e.message;
      if (e instanceof ConnectionError) update({ connection: "lost" });
    }
  };
  const addFiles = async (files) => {
    if (!files.length) return;
    await act(`Adding ${files.length} file(s) and profiling…`, async () => api.upload(await filesToPayload(files)), "Data added");
  };

  const fileInput = h("input", { type: "file", multiple: true, accept: ".csv,.xlsx,.xlsm,.json", class: "sr-only", id: "upload" });
  fileInput.addEventListener("change", () => { addFiles([...fileInput.files]); fileInput.value = ""; });
  const drop = h("div", { class: "dropzone", role: "button", tabindex: "0", "aria-describedby": "drop-help",
      onclick: () => fileInput.click(),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } } },
    h("span", { class: "btn btn--primary", "aria-hidden": "true" }, "Add files ", arrow()),
    h("span", { class: "meta", id: "drop-help" }, "or drop CSV / Excel files here. Files are added to your uploads; a file with the same name replaces the earlier one."));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("is-over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("is-over"); addFiles([...e.dataTransfer.files]); });

  const choice = (label, value, extra, disabled) => h("button", {
    type: "button", "aria-pressed": String(ds.workspace === value), disabled: disabled || null,
    onclick: () => ds.workspace !== value && act("Switching workspace…",
      value === "demonstration" ? api.useDemo : api.useUploads),
  }, label, extra != null && h("span", { class: "count" }, extra));

  main.replaceChildren(h("div", { class: "view" },
    h("header", { class: "view__head" },
      h("p", { class: "eyebrow" }, ds.workspace === "uploaded" ? "Datasets · your uploads" : "Datasets · demonstration data"),
      h("h1", { class: "title", tabindex: "-1" },
        `${ds.totals.tables} ${ds.totals.tables === 1 ? "table" : "tables"}, ${fmtInt(ds.totals.records)} ${ds.totals.records === 1 ? "record" : "records"}.`),
      h("div", { class: "workspace-bar" },
        h("div", {},
          h("p", { class: "eyebrow", style: { "margin-bottom": "var(--space-2)" } }, "Analyze"),
          h("div", { class: "segmented", role: "group", "aria-label": "Workspace" },
            choice("Demonstration data", "demonstration"),
            choice("Your uploads", "uploaded", uploads.length ? `${uploads.length}` : "0", !uploads.length))),
        h("p", { class: "meta", style: { "max-width": "38ch" } }, ds.workspace === "uploaded"
          ? "Questions run against every file you have uploaded."
          : uploads.length ? "Your uploads are kept. Switch back at any time." : "Upload files to analyze your own data.")),
      status),
    h("section", { class: "section", style: { "margin-top": "var(--space-6)" } },
      h("div", { class: "two-col" },
        h("div", {}, drop, fileInput),
        h("div", {},
          h("div", { class: "section__head" }, h("h2", { class: "eyebrow" }, "Your uploaded files"),
            h("span", { class: "meta" }, uploads.length ? `${uploads.length} kept` : "none yet")),
          uploads.length
            ? h("ul", { class: "uploads" }, uploads.map((u) => h("li", {},
                h("span", {}, u.name, " ", h("span", { class: "mono meta" }, `${fmtInt(Math.ceil(u.bytes / 1024))} KB`)),
                h("button", { class: "btn btn--secondary", type: "button", style: { "min-height": "30px" },
                  "aria-label": `Remove ${u.name}`,
                  onclick: () => act(`Removing ${u.name}…`, () => api.removeUpload(u.name), "File removed") }, "Remove"))))
            : h("p", { class: "meta" }, "Uploaded files stay here between sessions.")))),
    h("section", { class: "section" },
      h("div", { class: "section__head" }, h("h2", { class: "eyebrow" }, "Tables in use"),
        h("span", { class: "meta" }, ds.workspace === "uploaded" ? "From your uploads" : "Demonstration data")),
      h("div", { class: "rows", style: { "border-top": "0" } }, ds.tables.map((t, i) => reveal(
        h("a", { class: "row", href: `#/data/${encodeURIComponent(t.name)}` },
          h("span", { class: "row__index" }, pad(i + 1)),
          h("span", { class: "row__title" }, t.name, h("span", { class: "row__sub" }, t.key ? `key ${t.key}` : "no key detected")),
          h("span", { class: "row__meta num" }, `${fmtInt(t.rows)} rows · ${t.columns} cols`,
            t.issues ? h("span", { class: "row__sub" }, `${t.issues} issue${t.issues > 1 ? "s" : ""}`) : null),
          h("span", { class: "row__go" }, h("span", { class: "row__go-label" }, "Inspect"), arrow())), i)))),
    h("section", { class: "section" },
      h("div", { class: "section__head" }, h("h2", { class: "eyebrow" }, "Relationships"), h("span", { class: "meta" }, "Detected from key columns")),
      relationships(ds.relationships)),
    ds.metrics && Object.keys(ds.metrics).length > 0 && h("section", { class: "section" },
      h("div", { class: "section__head" }, h("h2", { class: "eyebrow" }, "Metric definitions"), h("span", { class: "meta" }, "From metrics.json")),
      table(["metric", "also called", "table", "definition"], Object.entries(ds.metrics).map(([k, d]) =>
        [k, (d.aliases || []).join(", ") || "—", d.table, d.description || (d.column ? `${d.aggregation || "sum"} of ${d.column}` : "rate")])))));
}

export function relationships(rels) {
  if (!rels.length) return h("p", { class: "meta" }, "No relationships detected.");
  return h("ul", { class: "relations" }, rels.map((r) => h("li", {},
    h("a", { class: "link", href: `#/data/${encodeURIComponent(r.parent)}` }, r.parent),
    h("span", { class: "rel-line" }, "──", h("span", { class: "rel-col" }, r.column), "──→"),
    h("a", { class: "link", href: `#/data/${encodeURIComponent(r.child)}` }, r.child),
    r.unmatched ? marker(`${r.unmatched} unmatched`, "fail") : marker("All matched", "pass"))));
}

const TABS = ["overview", "schema", "quality", "sample", "relationships"];

export async function renderDataset(main, { name, tab = "overview" }) {
  setTitle(`Dataset ${name}`);
  main.replaceChildren(h("div", { class: "view" }, h("p", { class: "eyebrow" }, `Dataset / ${name}`)));
  let d;
  try {
    d = await api.dataset(name);
  } catch (e) {
    if (e instanceof ConnectionError) { update({ connection: "lost" }); main.replaceChildren(connectionLost(() => location.reload())); }
    else main.replaceChildren(h("div", { class: "view" }, e instanceof ApiError && e.status === 404
      ? errorNotice(new Error(`There is no table named “${name}” in the current workspace.`))
      : errorNotice(e, () => renderDataset(main, { name, tab }))));
    return;
  }
  const panel = h("div", { role: "tabpanel", id: "dataset-panel", tabindex: "0" });
  const tabs = h("div", { class: "tabs", role: "tablist", "aria-label": "Dataset views" });
  const select = (t, focus) => {
    tabs.querySelectorAll("[role=tab]").forEach((b) => {
      const on = b.dataset.tab === t;
      b.setAttribute("aria-selected", on);
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    });
    panel.setAttribute("aria-labelledby", `tab-${t}`);
    panel.replaceChildren(reveal(TAB_RENDER[t](d)));
    history.replaceState(null, "", `#/data/${encodeURIComponent(name)}/${t}`);
  };
  TABS.forEach((t) => tabs.append(h("button", { class: "tab", role: "tab", id: `tab-${t}`, "data-tab": t, "aria-controls": "dataset-panel",
    type: "button", onclick: () => select(t) }, t)));
  tabs.addEventListener("keydown", (e) => {
    const i = TABS.indexOf(tabs.querySelector("[aria-selected=true]").dataset.tab);
    if (e.key === "ArrowRight") select(TABS[(i + 1) % TABS.length], true);
    if (e.key === "ArrowLeft") select(TABS[(i - 1 + TABS.length) % TABS.length], true);
  });

  main.replaceChildren(h("div", { class: "view" },
    h("header", { class: "view__head" },
      h("p", { class: "eyebrow" }, h("a", { class: "link", href: "#/data" }, "Dataset"), " / ", d.name),
      h("h1", { class: "title", tabindex: "-1" }, d.name),
      h("p", { class: "lead num" }, `${fmtInt(d.rows)} rows · ${d.columns.length} columns · ${d.key ? `key ${d.key}` : "no key detected"}`)),
    tabs, panel));
  select(TABS.includes(tab) ? tab : "overview");
}

const KIND = (c) => c.kind === "decimal" ? `decimal (${c.decimals} dp)`
  : c.date_format ? `${c.kind} (${c.date_format})` : c.kind === "text" && Object.keys(c.top_values).length ? "category" : c.kind;

const TAB_RENDER = {
  overview: (d) => h("div", {},
    facts([
      ["Rows", fmtInt(d.rows)], ["Columns", String(d.columns.length)], ["Key", d.key || "none detected"],
      ["Exact duplicate rows", fmtInt(d.exact_duplicate_rows)],
      ["Conflicting keys", d.duplicate_keys.length ? `${d.duplicate_keys.length} (${d.duplicate_keys.slice(0, 5).join(", ")})` : "0"],
      ["Columns with missing values", String(d.columns.filter((c) => c.nulls).length)],
      ["Data issues", String(d.issues.length)],
    ])),
  schema: (d) => table(["column", "type", "null %", "unique", "min", "max", "example"],
    d.columns.map((c) => [c.name, KIND(c), `${c.null_pct}%`, fmtInt(c.unique), c.min, c.max, c.samples[0]]),
    { numeric: ["null %", "unique"], mono: ["min", "max", "example"], clip: ["example", "min", "max"], caption: `Schema of ${d.name}` }),
  quality: (d) => d.issues.length ? h("div", {}, d.issues.map(issueStory)) : h("p", { class: "meta" }, "No data-quality issues detected in this table."),
  sample: (d) => h("div", {}, h("p", { class: "meta", style: { "margin-bottom": "var(--space-3)" } }, `First ${d.sample.rows.length} of ${fmtInt(d.rows)} rows, as stored.`),
    table(d.sample.columns, d.sample.rows, { clip: d.sample.columns, caption: `Sample rows of ${d.name}` })),
  relationships: (d) => relationships(d.relationships),
};

