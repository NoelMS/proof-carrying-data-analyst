// Analysis history: an editorial list grouped by day.
import { arrow, h, pad, setTitle } from "../dom.js";
import { reveal } from "../motion.js";
import { store } from "../state.js";
import { STATE_LABEL } from "./analysis.js";

const day = (ts) => new Date(ts * 1000).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }).toUpperCase();

export function renderHistory(main) {
  setTitle("History");
  const items = store.history;
  if (!items) { main.replaceChildren(h("div", { class: "view" }, h("p", { class: "eyebrow" }, "Loading history"))); return; }
  if (!items.length) {
    main.replaceChildren(h("div", { class: "view" }, h("section", { class: "notice" },
      h("p", { class: "eyebrow" }, "No analyses yet"),
      h("h1", { class: "section-title", tabindex: "-1" }, "Ask your first analytical question to begin."),
      h("a", { class: "btn btn--primary", href: "#/" }, "Ask a question ", arrow()))));
    return;
  }
  const groups = new Map();
  for (const it of items) {
    const k = day(it.ts);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(it);
  }
  let n = 0;
  main.replaceChildren(h("div", { class: "view" },
    h("header", { class: "view__head" },
      h("p", { class: "eyebrow" }, "Analysis history"),
      h("h1", { class: "title", tabindex: "-1" }, `${pad(items.length)} analyses.`),
      h("p", { class: "lead" }, `${items.filter((i) => i.status === "verified").length} verified, ${items.filter((i) => i.status === "refused").length} refused.`)),
    [...groups].map(([date, list]) => h("section", { class: "section", style: { "margin-top": "var(--space-6)" } },
      h("h2", { class: "eyebrow", style: { "margin-bottom": "var(--space-3)" } }, date),
      h("div", { class: "rows" }, list.map((it) => reveal(h("a", { class: "row", href: `#/analysis/${it.id}` },
        h("span", { class: "row__index" }, pad(++n)),
        h("span", { class: "row__title" }, it.question,
          h("span", { class: "row__sub" }, it.status === "verified" ? it.answer : it.status === "refused" ? `${STATE_LABEL[it.answerability] || "Refused"}: ${it.reason}` : "In progress")),
        h("span", { class: "row__meta" },
          h("span", { class: `marker marker--${it.status === "verified" ? "verified" : it.status === "refused" ? "fail" : "active"}` },
            it.status === "verified" ? "Verified" : it.status === "refused" ? "Refused" : "Running"),
          h("span", { class: "row__sub num" }, `${it.duration_s.toFixed(2)} s`)),
        h("span", { class: "row__go" }, h("span", { class: "row__go-label" }, "Open"), arrow())), Math.min(n, 8))))))));
}
