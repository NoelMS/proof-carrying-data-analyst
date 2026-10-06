// Loaded synchronously in <head> so a saved light theme applies before first paint. Dark is the default.
try {
  if (localStorage.getItem("pcda-theme") === "light") document.documentElement.dataset.theme = "light";
} catch { /* storage unavailable: keep the default */ }
