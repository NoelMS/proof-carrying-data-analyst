// Reusable motion primitives. All of them are no-ops or instant under reduced motion.

const media = window.matchMedia("(prefers-reduced-motion: reduce)");
export const reducedMotion = () => media.matches;

const io = "IntersectionObserver" in window
  ? new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) { e.target.classList.add("is-in"); io.unobserve(e.target); }
    }, { rootMargin: "0px 0px -8% 0px" })
  : null;

/** Masked reveal when the element enters the viewport. `i` staggers siblings. */
export function reveal(el, i = 0) {
  if (reducedMotion() || !io) { el.classList.add("is-in"); return el; }
  el.classList.add("reveal");
  el.style.setProperty("--i", i);
  io.observe(el);
  return el;
}

/** Mark an element as entered on the next frame (for transitions that should run immediately). */
export function enter(el) {
  if (reducedMotion()) { el.classList.add("is-in"); return el; }
  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add("is-in")));
  return el;
}

/** Swap the contents of a container with an exit/enter transition. */
export function swap(container, next) {
  const prev = container.firstElementChild;
  const show = () => {
    container.replaceChildren(next);
    if (reducedMotion()) return;
    next.classList.add("is-entering");
    requestAnimationFrame(() => requestAnimationFrame(() => {
      next.classList.remove("is-entering");
      next.classList.add("is-entered");
    }));
  };
  if (!prev || reducedMotion()) return show();
  prev.classList.add("is-leaving");
  setTimeout(show, 200);
}

/** Route changes: View Transitions API where supported, plain update otherwise. */
export function transition(update) {
  if (!document.startViewTransition || reducedMotion()) return Promise.resolve(update());
  return document.startViewTransition(update).finished.catch(() => {});
}

/**
 * Count up to a backend-formatted number once, then set the exact backend text.
 * "815,497.70 USD" -> animates 0 .. 815497.70 with the same grouping and decimals.
 */
export function animateNumber(el, finalText, duration = 900) {
  el.textContent = finalText;
  const m = /^(-?)([\d,]+)(\.\d+)?(.*)$/.exec(finalText);
  if (!m || reducedMotion()) return;
  const [, sign, int, frac = "", suffix] = m;
  const target = Number(int.replace(/,/g, "") + frac);
  const decimals = frac ? frac.length - 1 : 0;
  const fmt = new Intl.NumberFormat("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals,
                                                useGrouping: int.includes(",") });
  const start = performance.now();
  const ease = (t) => 1 - Math.pow(1 - t, 4);
  const tick = (now) => {
    const t = Math.min(1, (now - start) / duration);
    if (t < 1) {
      el.textContent = sign + fmt.format(target * ease(t)) + suffix;
      requestAnimationFrame(tick);
    } else {
      el.textContent = finalText; // the exact value from the backend, always
    }
  };
  requestAnimationFrame(tick);
}

/** Thin scroll-progress line + condensed header. */
export function initScrollEffects(header, line) {
  let queued = false;
  const update = () => {
    queued = false;
    const max = document.documentElement.scrollHeight - innerHeight;
    line.style.setProperty("--progress", max > 0 ? (scrollY / max).toFixed(4) : 0);
    header.classList.toggle("is-condensed", scrollY > 24);
  };
  addEventListener("scroll", () => { if (!queued) { queued = true; requestAnimationFrame(update); } }, { passive: true });
  update();
}
