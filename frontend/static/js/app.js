/* ============================================================
   app.js — shared shell: collapsible sidebar (persisted).
   Collapse animates grid width once on click; labels fade via
   opacity and icons stay pinned, so nothing jumps. No polling.
   ============================================================ */
(() => {
  const SB_KEY = "ai_cam_sb_collapsed";
  const shell = document.querySelector(".app-shell");
  const mq = window.matchMedia("(max-width:860px)");
  const isMobile = () => mq.matches;

  function apply() {
    if (!shell) return;
    if (isMobile()) {
      // Mobile: off-canvas drawer only — desktop icon-collapse state must not apply here.
      shell.classList.remove("sb-collapsed");
    } else {
      shell.classList.remove("sb-mobile-open");
      shell.classList.toggle("sb-collapsed", localStorage.getItem(SB_KEY) === "1");
    }
  }
  apply(); // before paint

  document.addEventListener("DOMContentLoaded", () => {
    apply();
    const burger = document.getElementById("burger");
    const mobileBurger = document.getElementById("mobileBurger");
    const scrim = document.getElementById("sbScrim");

    function toggleSidebar() {
      if (!shell) return;
      if (isMobile()) {
        shell.classList.toggle("sb-mobile-open");
      } else {
        const c = shell.classList.toggle("sb-collapsed");
        localStorage.setItem(SB_KEY, c ? "1" : "0");
      }
    }
    function closeMobileDrawer() {
      if (shell) shell.classList.remove("sb-mobile-open");
    }

    if (burger) burger.addEventListener("click", toggleSidebar);
    if (mobileBurger) mobileBurger.addEventListener("click", toggleSidebar);
    if (scrim) scrim.addEventListener("click", closeMobileDrawer);

    // Tapping a nav link on mobile should close the drawer behind it.
    document.querySelectorAll(".sb-link").forEach((a) => {
      a.addEventListener("click", () => { if (isMobile()) closeMobileDrawer(); });
    });

    // Keep state correct when crossing the breakpoint (e.g. rotating a tablet).
    mq.addEventListener ? mq.addEventListener("change", apply) : mq.addListener(apply);

    // About dialog
    const about = document.getElementById("aboutModal");
    const openBtn = document.getElementById("aboutBtn");
    const closeBtn = document.getElementById("aboutClose");
    if (about && openBtn) {
      openBtn.addEventListener("click", () => about.classList.add("show"));
      closeBtn && closeBtn.addEventListener("click", () => about.classList.remove("show"));
      about.addEventListener("click", (e) => { if (e.target === about) about.classList.remove("show"); });
    }
  });
})();
