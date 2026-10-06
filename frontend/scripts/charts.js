// Restrained SVG charts for grouped and ranked results.
// Bars are drawn from verified values; labels show the backend-formatted text.
import { h, svg } from "./dom.js";
import { reveal } from "./motion.js";

/**
 * items: [{ key, value, formatted }]
 * columns: true for chronological series (vertical columns), false for comparisons (horizontal bars).
 */
export function barChart(items, { columns = false, title = "Result chart" } = {}) {
  const values = items.map((d) => Number(d.value));
  if (!items.length || values.some((v) => !Number.isFinite(v)) || values.some((v) => v < 0)) return null;
  const max = Math.max(...values) || 1;
  const top = items[values.indexOf(Math.max(...values))].key;
  const el = columns ? columnsSvg(items, values, max, top) : barsSvg(items, values, max, top);
  el.setAttribute("role", "img");
  el.setAttribute("aria-label", `${title}. Highest: ${top}. Exact values are listed in the table below.`);
  const fig = h("figure", { class: `chart${columns ? " chart--columns" : ""}` }, el);
  return reveal(fig);
}

function barsSvg(items, values, max, top) {
  const rowH = 30, labelW = 150, valueW = 130, w = 640, barW = w - labelW - valueW;
  const s = svg("svg", { viewBox: `0 0 ${w} ${items.length * rowH + 8}` });
  items.forEach((d, i) => {
    const y = i * rowH + 4;
    const label = svg("text", { x: 0, y: y + 15 });
    label.textContent = d.key.length > 22 ? d.key.slice(0, 21) + "…" : d.key;
    const bar = svg("rect", { x: labelW, y: y + 4, height: 14, width: Math.max(1, (values[i] / max) * barW),
                              class: `chart__bar${d.key === top ? " is-top" : ""}`, style: { "--i": i } });
    const val = svg("text", { x: labelW + (values[i] / max) * barW + 8, y: y + 15, class: "chart__value" });
    val.textContent = d.formatted;
    s.append(label, bar, val);
  });
  s.append(svg("line", { x1: labelW, x2: labelW, y1: 0, y2: items.length * rowH + 8, class: "chart__axis" }));
  return s;
}

function columnsSvg(items, values, max, top) {
  const w = 640, hgt = 220, base = 190, gap = 6;
  const colW = (w - gap * (items.length - 1)) / items.length;
  const s = svg("svg", { viewBox: `0 -18 ${w} ${hgt + 18}` });
  items.forEach((d, i) => {
    const x = i * (colW + gap);
    const bh = Math.max(1, (values[i] / max) * (base - 10));
    s.append(svg("rect", { x, y: base - bh, width: colW, height: bh,
                           class: `chart__bar${d.key === top ? " is-top" : ""}`, style: { "--i": i } }));
    const label = svg("text", { x: x + colW / 2, y: base + 18, "text-anchor": "middle" });
    label.textContent = /^\d{4}-\d{2}$/.test(d.key)
      ? new Date(`${d.key}-01T00:00:00`).toLocaleString("en-US", { month: "short" }) : d.key.slice(0, 8);
    s.append(label);
    if (d.key === top) {
      const val = svg("text", { x: x + colW / 2, y: base - bh - 6, "text-anchor": "middle", class: "chart__value" });
      val.textContent = d.formatted;
      s.append(val);
    }
  });
  s.append(svg("line", { x1: 0, x2: w, y1: base, y2: base, class: "chart__axis" }));
  return s;
}
