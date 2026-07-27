/* ============================================================
   theme.js — 3 themes: light · dark · glass (Liquid Glass).
   Default = glass. Persisted in localStorage. Syncs every
   .theme-seg control (topbar + sidebar) across all pages.
   ============================================================ */
(() => {
  const KEY = "ai_cam_theme";
  const THEMES = ["light", "dark", "glass"];

  function current() {
    return localStorage.getItem(KEY) || "glass";
  }
  // Apply immediately (before paint) to avoid flash
  document.documentElement.setAttribute("data-theme", current());

  function sync() {
    const t = current();
    document.querySelectorAll(".theme-seg button[data-theme]").forEach((b) => {
      b.classList.toggle("active", b.dataset.theme === t);
    });
  }

  window.setTheme = function (t) {
    if (!THEMES.includes(t)) t = "glass";
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem(KEY, t);
    sync();
  };

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".theme-seg button[data-theme]").forEach((b) => {
      b.addEventListener("click", () => window.setTheme(b.dataset.theme));
    });
    sync();
  });
})();
