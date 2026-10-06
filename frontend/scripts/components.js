// Shared UI components.
import { arrow, h } from "./dom.js";

/* ---------------------------------------------------------------- toast */
export function toast(message, kind = "info") {
  const el = h("div", { class: `toast${kind === "error" ? " toast--error" : ""}` }, message);
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), 2600);
}

export function announce(message) {
  const el = document.getElementById("announcer");
  el.textContent = "";
  requestAnimationFrame(() => { el.textContent = message; });
}

/* ---------------------------------------------------------------- drawer */
let lastFocus = null;

export function openDrawer(title, content) {
  const drawer = document.getElementById("drawer");
  document.getElementById("drawer-title").textContent = title;
  document.getElementById("drawer-body").replaceChildren(...[content].flat());
  lastFocus = document.activeElement;
  drawer.hidden = false;
  document.body.style.setProperty("overflow", "hidden");
  requestAnimationFrame(() => requestAnimationFrame(() => drawer.classList.add("is-open")));
  drawer.querySelector(".drawer__head button").focus();
}

export function closeDrawer() {
  const drawer = document.getElementById("drawer");
  if (drawer.hidden) return false;
  drawer.classList.remove("is-open");
  document.body.style.removeProperty("overflow");
  setTimeout(() => { drawer.hidden = true; }, 300);
  lastFocus?.focus?.();
  return true;
}

export function initDrawer() {
  const drawer = document.getElementById("drawer");
  drawer.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeDrawer(); });
  drawer.addEventListener("keydown", (e) => {  // keep focus inside the open dialog
    if (e.key !== "Tab") return;
    const f = [...drawer.querySelectorAll("button, a[href], summary, [tabindex]:not([tabindex='-1'])")].filter((x) => x.offsetParent);
    if (!f.length) return;
    if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f.at(-1).focus(); }
    else if (!e.shiftKey && document.activeElement === f.at(-1)) { e.preventDefault(); f[0].focus(); }
  });
}

/* ---------------------------------------------------------------- notices */
export function notice({ eyebrow, title, text, action, kind }) {
  return h("section", { class: `notice${kind === "error" ? " notice--error" : ""}` },
    h("p", { class: "eyebrow" }, eyebrow),
    title && h("h1", { class: "section-title", tabindex: "-1" }, title),
    text && h("p", {}, text),
    action);
}

export function connectionLost(retry) {
  return notice({
    kind: "error", eyebrow: "Connection lost", title: "The analytical service is currently unavailable.",
    text: "Nothing is running in the background. Start the server and retry.",
    action: h("button", { class: "btn btn--primary", type: "button", onclick: retry }, "Retry ", arrow()),
  });
}

export function errorNotice(err, retry) {
  return notice({
    kind: "error", eyebrow: "Request failed", title: err.message,
    action: retry && h("button", { class: "btn btn--secondary", type: "button", onclick: retry }, "Retry ", arrow()),
  });
}

/* ---------------------------------------------------------------- small pieces */
export const marker = (text, kind = "neutral") => h("span", { class: `marker marker--${kind}` }, text);

export function facts(pairs) {
  return h("dl", { class: "facts" }, pairs.filter(Boolean).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}

export function table(columns, rows, { numeric = [], mono = [], clip = [], caption } = {}) {
  const cls = (c) => [numeric.includes(c) && "is-num", mono.includes(c) && "is-mono", clip.includes(c) && "is-clip"].filter(Boolean).join(" ") || null;
  return h("div", { class: "table-wrap" },
    h("table", { class: "table" },
      caption && h("caption", { class: "sr-only" }, caption),
      h("thead", {}, h("tr", {}, columns.map((c) => h("th", { scope: "col", class: cls(c) }, c)))),
      h("tbody", {}, rows.map((r) => h("tr", {}, r.map((v, i) =>
        h("td", { class: cls(columns[i]), title: clip.includes(columns[i]) ? String(v ?? "") : null }, v ?? "—")))))));
}

export function codeBlock(code) {
  const pre = h("pre", { class: "code", tabindex: "0", "aria-label": "Proof code" });
  const lines = code.replace(/\n$/, "").split("\n");
  pre.append(h("code", {}, lines.map((ln) => h("span", { class: "code__line" }, ln + "\n"))));
  return pre;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function download(filename, text, type = "text/plain") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
