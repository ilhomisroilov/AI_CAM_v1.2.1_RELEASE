/* ============================================================
   settings.js — load/edit/save config/settings.yaml.
   Two modes: category Form (structured) and Raw YAML editor.
   ============================================================ */
(() => {
  const $ = (id) => document.getElementById(id);
  let data = {};   // current parsed settings

  function toast(msg, ok) {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast show " + (ok ? "ok" : "err");
    setTimeout(() => (t.className = "toast"), 2600);
  }

  const getPath = (o, p) => p.split(".").reduce((a, k) => (a == null ? undefined : a[k]), o);
  function setPath(o, p, v) {
    const ks = p.split("."); let cur = o;
    for (let i = 0; i < ks.length - 1; i++) { if (typeof cur[ks[i]] !== "object" || cur[ks[i]] == null) cur[ks[i]] = {}; cur = cur[ks[i]]; }
    cur[ks[ks.length - 1]] = v;
  }
  function coerce(el, raw) {
    const t = el.dataset.type;
    if (t === "int") { const n = parseInt(raw, 10); return isNaN(n) ? 0 : n; }
    if (t === "float") { const n = parseFloat(raw); return isNaN(n) ? 0 : n; }
    if (t === "bool") return raw === "true" || raw === true;
    return raw;
  }

  function populateForm(src) {
    document.querySelectorAll("[data-path]").forEach((el) => {
      let v = getPath(src, el.dataset.path);
      if (v === undefined || v === null) return;
      if (el.dataset.type === "bool") v = v ? "true" : "false";
      el.value = v;
    });
  }
  function collectForm() {
    const out = JSON.parse(JSON.stringify(data || {}));
    document.querySelectorAll("[data-path]").forEach((el) => {
      setPath(out, el.dataset.path, coerce(el, el.value));
    });
    return out;
  }

  async function load() {
    try {
      const r = await (await fetch("/api/settings")).json();
      data = r.data || {};
      populateForm(data);
      $("yamlEditor").value = r.raw || "";
      $("yamlStatus").textContent = "";
    } catch (e) { toast("Failed to load settings", false); }
  }

  async function postSettings(body, okMsg) {
    try {
      const r = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const j = await r.json();
      if (j.ok) { toast(j.note || okMsg, true); load(); }
      else toast(j.error || "Save failed", false);
    } catch (e) { toast("Network error", false); }
  }

  // ---- tabs ----
  function showTab(which) {
    const form = which === "form";
    $("panelForm").style.display = form ? "" : "none";
    $("panelYaml").style.display = form ? "none" : "";
    $("tabForm").classList.toggle("active", form);
    $("tabYaml").classList.toggle("active", !form);
  }
  $("tabForm").addEventListener("click", () => showTab("form"));
  $("tabYaml").addEventListener("click", () => showTab("yaml"));

  // ---- actions ----
  $("btnSave").addEventListener("click", () => postSettings({ data: collectForm() }, "Saved"));
  $("btnReload").addEventListener("click", load);
  $("btnSaveYaml").addEventListener("click", () => postSettings({ raw: $("yamlEditor").value }, "Saved"));

  $("btnValidate").addEventListener("click", () => {
    const txt = $("yamlEditor").value;
    if (/\t/.test(txt)) { $("yamlStatus").textContent = "⚠ Tabs are not allowed in YAML — use spaces."; return; }
    $("yamlStatus").textContent = "Looks ok — server validates on save.";
  });

  $("btnDefaults").addEventListener("click", async () => {
    if (!confirm("Load factory defaults into the form? You still need to Save to apply.")) return;
    try {
      const r = await (await fetch("/api/settings/defaults")).json();
      data = r.data || {};
      populateForm(data);
      showTab("form");
      toast("Defaults loaded — review, then Save", true);
    } catch (e) { toast("Failed to load defaults", false); }
  });

  load();
})();
