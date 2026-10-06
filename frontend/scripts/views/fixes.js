// Data fixes: choose, preview the exact changes, confirm, and roll back. The backend applies nothing
// until "Apply change" is pressed, and rejects the apply if the table changed after the preview.
import { ConnectionError, api } from "../api.js";
import { closeDrawer, openDrawer, toast } from "../components.js";
import { arrow, fmtInt, h } from "../dom.js";
import { update } from "../state.js";

const errorText = (e) => (e instanceof ConnectionError ? "Connection lost." : e.message);

async function refreshAfter(result, message) {
  update({ datasets: result.datasets, fixes: result.fixes });
  toast(message);
}

export function openFixDrawer(opt) {
  const status = h("p", { class: "meta", "aria-live": "polite" });
  const preview = h("div");
  const name = `fix-${opt.id}`;
  const choices = opt.choices.length > 0 && h("fieldset", { class: "fix-choices", style: { border: "0", padding: "0" } },
    h("legend", { class: "eyebrow", style: { "margin-bottom": "var(--space-2)" } }, "Choose how to fix it"),
    opt.choices.map((c) => h("label", { class: "fix-choice" },
      h("input", { type: "radio", name, value: c.value }), h("span", {}, c.label))));
  const input = opt.input && h("input", { class: "field", type: "text", "aria-label": opt.input.label, placeholder: opt.input.label });
  const selected = () => ({
    choice: choices ? choices.querySelector("input:checked")?.value ?? null : null,
    value: input ? input.value : null,
  });

  const previewBtn = h("button", { class: "btn btn--primary", type: "button" }, "Preview change ", arrow());
  previewBtn.addEventListener("click", async () => {
    const { choice, value } = selected();
    previewBtn.disabled = true;
    status.textContent = "Computing the changes…";
    try {
      const p = await api.previewFix(opt.id, choice, value);
      status.textContent = "";
      preview.replaceChildren(renderPreview(p, opt));
      preview.querySelector("[data-apply]")?.focus();
    } catch (e) {
      status.textContent = errorText(e);
    }
    previewBtn.disabled = false;
  });
  // changing the choice invalidates the shown preview
  choices?.addEventListener("change", () => preview.replaceChildren());
  input?.addEventListener("input", () => preview.replaceChildren());

  openDrawer("Fix data", [
    h("section", {},
      h("p", { class: "eyebrow" }, `${opt.table}${opt.column ? "." + opt.column : ""}`),
      h("h3", { class: "section-title", style: { margin: "var(--space-2) 0 var(--space-3)" } }, opt.title),
      h("p", { class: "lead", style: { "font-size": "15px" } }, opt.description),
      choices, input && h("div", { style: { "margin-bottom": "var(--space-4)" } }, input),
      h("div", { class: "btn-row" }, previewBtn, h("button", { class: "btn btn--secondary", type: "button", onclick: closeDrawer }, "Cancel")),
      status),
    preview,
  ]);
}

function renderPreview(p, opt) {
  const applyBtn = h("button", { class: "btn btn--primary", type: "button", "data-apply": "" }, "Apply change ", arrow());
  const status = h("p", { class: "meta", "aria-live": "polite" });
  const nothing = !p.removed.count && !p.changed.count;
  applyBtn.disabled = nothing;
  applyBtn.addEventListener("click", async () => {
    applyBtn.disabled = true;
    status.textContent = "Applying and re-profiling…";
    try {
      await refreshAfter(await api.applyFix(opt.id, p.choice, p.value, p.token), "Change applied");
      closeDrawer();
    } catch (e) {
      status.textContent = errorText(e);
      applyBtn.disabled = false;
    }
  });
  return h("section", {},
    h("p", { class: "eyebrow" }, "Preview"),
    h("div", { class: "diff-summary" },
      h("div", {}, h("p", { class: "tally__v num" }, `${fmtInt(p.rows_before)} → ${fmtInt(p.rows_after)}`), h("p", { class: "meta" }, "rows")),
      h("div", {}, h("p", { class: "tally__v num" }, fmtInt(p.removed.count)), h("p", { class: "meta" }, "rows removed")),
      h("div", {}, h("p", { class: "tally__v num" }, fmtInt(p.changed.count)), h("p", { class: "meta" }, "cells changed"))),
    p.changed.count > 0 && h("div", { style: { "margin-bottom": "var(--space-5)" } },
      h("p", { class: "eyebrow", style: { "margin-bottom": "var(--space-2)" } },
        `Changed cells${p.changed.count > p.changed.cells.length ? ` (first ${p.changed.cells.length} of ${fmtInt(p.changed.count)})` : ""}`),
      changedTable(p.changed.cells)),
    p.removed.count > 0 && h("div", { style: { "margin-bottom": "var(--space-5)" } },
      h("p", { class: "eyebrow", style: { "margin-bottom": "var(--space-2)" } },
        `Removed rows${p.removed.count > p.removed.rows.length ? ` (first ${p.removed.rows.length} of ${fmtInt(p.removed.count)})` : ""}`),
      removedTable(p.removed)),
    h("p", { class: "notice-inline", style: { "margin-bottom": "var(--space-4)" } }, nothing
      ? "This choice would not change any rows."
      : "Nothing has been changed yet. Applying keeps a copy of the current table, so the change can be rolled back from Change history."),
    h("div", { class: "btn-row" }, applyBtn, h("button", { class: "btn btn--secondary", type: "button", onclick: closeDrawer }, "Cancel")),
    status);
}

function changedTable(cells) {
  return h("div", { class: "table-wrap", style: { "max-height": "320px" } }, h("table", { class: "table" },
    h("caption", { class: "sr-only" }, "Changed cells"),
    h("thead", {}, h("tr", {}, ["row", "column", "before", "after"].map((c) => h("th", { scope: "col" }, c)))),
    h("tbody", {}, cells.map((c) => h("tr", {},
      h("td", { class: "is-mono" }, c.row), h("td", {}, c.column),
      h("td", { class: "is-mono" }, h("span", { class: "cell-before" }, c.before === "" ? "(empty)" : c.before)),
      h("td", { class: "is-mono" }, h("span", { class: "cell-after" }, c.after === "" ? "(empty)" : c.after)))))));
}

function removedTable(removed) {
  return h("div", { class: "table-wrap", style: { "max-height": "320px" } }, h("table", { class: "table" },
    h("caption", { class: "sr-only" }, "Removed rows"),
    h("thead", {}, h("tr", {}, removed.columns.map((c) => h("th", { scope: "col" }, c)))),
    h("tbody", {}, removed.rows.map((r) => h("tr", { class: "is-removed" }, r.map((v) => h("td", { class: "is-clip", title: v }, v)))))));
}

export function changeHistory(fixes) {
  const log = fixes?.log || [];
  if (!log.length) return h("p", { class: "meta" }, "No changes have been made to this workspace's data. Source files are never modified.");
  const latestPerTable = new Map();
  log.forEach((e) => latestPerTable.set(e.table, e.id));
  const tables = [...new Set(log.map((e) => e.table))];
  const act = async (btn, call, message) => {
    btn.disabled = true;
    try { await refreshAfter(await call(), message); }
    catch (e) { toast(errorText(e), "error"); btn.disabled = false; }
  };
  return h("div", {},
    h("ul", { class: "uploads" }, [...log].reverse().map((e) => {
      const rb = h("button", { class: "btn btn--secondary", type: "button", style: { "min-height": "30px" },
        disabled: latestPerTable.get(e.table) !== e.id || null,
        title: latestPerTable.get(e.table) !== e.id ? "Roll back the later change to this table first" : null }, "Roll back");
      rb.addEventListener("click", () => act(rb, () => api.rollbackFix(e.id), "Change rolled back"));
      return h("li", {},
        h("div", {},
          h("p", {}, h("strong", {}, e.title), " ", h("span", { class: "meta" }, `· ${e.table}`)),
          h("p", { class: "meta" }, `${new Date(e.ts * 1000).toLocaleString()} · ${e.summary} · rows ${fmtInt(e.rows_before)} → ${fmtInt(e.rows_after)}`
            + (e.choice ? ` · option: ${e.choice}` : "") + (e.value ? ` · value: ${e.value}` : ""))),
        rb);
    })),
    h("div", { class: "btn-row", style: { "margin-top": "var(--space-4)" } },
      h("span", { class: "meta" }, "Restore original:"),
      tables.map((t) => {
        const b = h("button", { class: "btn btn--secondary", type: "button", style: { "min-height": "30px" } }, t);
        b.addEventListener("click", () => {
          if (confirm(`Undo every change to ${t} and return to the original data?`)) act(b, () => api.restoreTable(t), `${t} restored`);
        });
        return b;
      })));
}

export function fixButton(opt) {
  return h("button", { class: "btn btn--secondary story__fix", type: "button", onclick: () => openFixDrawer(opt) },
    "Fix this issue ", arrow());
}

export function fixFor(issue, fixes) {
  return (fixes?.options || []).find((o) => o.kind === issue.kind && o.table === issue.table && (o.column || null) === (issue.column || null));
}

