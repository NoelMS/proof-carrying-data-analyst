// Element construction. Data is always inserted as text, never as HTML.

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of [children].flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const svg = (tag, attrs = {}) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "style") for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
    else el.setAttribute(k, v);
  }
  return el;
};

export const pad = (n, width = 2) => String(n).padStart(width, "0");
export const fmtInt = (n) => new Intl.NumberFormat("en-US").format(n);
export const arrow = () => h("span", { class: "arrow", "aria-hidden": "true" }, "→");
export const label = (s) => String(s).replace(/_/g, " ");

export function setTitle(part) {
  document.title = part ? `${part} · Proof-Carrying Data Analyst` : "Proof-Carrying Data Analyst";
}
