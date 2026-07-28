/* ============================================================
   history.js — filters (VIN/EPC/model/score/date), stats row,
   sortable sticky table, pagination, image modal, CSV/Excel export.
   ============================================================ */
(() => {
  const $ = (id) => document.getElementById(id);
  const T = (k) => (window.t ? window.t(k) : k);
  const recBody = $("recBody");
  const PAGE = 25;

  let sortBy = "timestamp", order = "DESC";
  let rows = [], page = 1;
  let resetOnNextLoad = true;

  const normTs = (v) => (v || "").replace("T", " ").trim();
  const esc = (s) => (s == null ? "" : String(s).replace(/[<>&]/g, ""));

  function filterParams() {
    const p = new URLSearchParams();
    const vin = ($("searchVin")?.value || "").trim();
    const epc = ($("searchEpc")?.value || "").trim();
    const model = ($("filterModel")?.value || "").trim();
    const score = ($("filterScore")?.value || "0").trim();
    const start = normTs($("dateStart")?.value);
    const end = normTs($("dateEnd")?.value);
    if (vin) p.set("vin", vin);
    if (epc) p.set("epc", epc);
    if (model) p.set("model", model);
    if (score && score !== "0") p.set("min_score", score);
    if (start) p.set("start", start);
    if (end) p.set("end", end);
    return p;
  }

  function confBar(c) {
    const pct = Math.round((c || 0) * 100);
    return `<div class="row" style="gap:8px"><div class="conf-bar"><span style="width:${pct}%"></span></div><small class="muted">${pct}%</small></div>`;
  }

  function statusBadge(st) {
    if (st === "SUCCESS") return `<span class="badge green">SUCCESS</span>`;
    if (st === "TIMEOUT") return `<span class="badge amber">TIMEOUT</span>`;
    if (st === "OK") return `<span class="badge blue">OK</span>`;
    if (st === "VIN_OK_RFID_TIMEOUT") return `<span class="badge amber">VIN✓ / RFID✗</span>`;
    return `<span class="muted">${esc(st) || "—"}</span>`;
  }

  function rfidCell(r) {
    if (!r.rfid_epc) return `<span class="muted">—</span>`;
    if (r.rfid_epc === "NO_TAG") return `<span class="rfid-flag warn">NO_TAG</span>`;
    if (r.rfid_epc === "NO_READ") return `<span class="rfid-flag bad">NO_READ</span>`;
    return `<span class="vin-cell" title="${esc(r.rfid_raw) || ""}">${esc(r.rfid_epc)}</span>`;
  }

  function computeStats() {
    const today = new Date().toISOString().slice(0, 10);
    let todayN = 0, rfidN = 0, vinOk = 0;
    for (const r of rows) {
      if ((r.timestamp || "").slice(0, 10) === today) todayN++;
      if (r.rfid_epc && !["NO_TAG", "NO_READ"].includes(r.rfid_epc)) rfidN++;
      if (r.detected_vin && r.detected_vin !== "NO_READ") vinOk++;
    }
    $("stTotal").textContent = rows.length;
    $("stToday").textContent = todayN;
    $("stRfid").textContent = rfidN;
    $("stRate").textContent = rows.length ? Math.round((vinOk / rows.length) * 100) + "%" : "0%";
  }

  function renderPage() {
    const pages = Math.max(1, Math.ceil(rows.length / PAGE));
    if (page > pages) page = pages;
    const slice = rows.slice((page - 1) * PAGE, page * PAGE);
    $("shownCount").textContent = slice.length;
    $("totalCount").textContent = rows.length;
    $("pageInfo").textContent = `${T("hist.page")} ${page} / ${pages}`;
    $("pgPrev").disabled = page <= 1;
    $("pgNext").disabled = page >= pages;

    if (!slice.length) {
      recBody.innerHTML = `<tr><td colspan="9" class="empty">${T("hist.none")}</td></tr>`;
      return;
    }
    recBody.innerHTML = slice.map((r) => {
      const img = r.image_path
        ? `<img class="thumb" data-full="/${r.image_path}" data-cap="${esc(r.detected_vin)} · ${esc(r.timestamp)}" src="/${r.image_path}" alt="">`
        : `<span class="muted">—</span>`;
      const model = r.model ? `<span class="model-badge">${esc(r.model)}</span>` : `<span class="muted">—</span>`;
      const raw = (r.raw_vin && r.raw_vin !== r.detected_vin)
        ? `<span class="mono" style="color:var(--warning)">${esc(r.raw_vin)}</span>`
        : `<span class="muted">${esc(r.raw_vin) || "—"}</span>`;
      return `<tr>
        <td>${r.id}</td>
        <td class="mono" style="font-size:12.5px">${esc(r.timestamp)}</td>
        <td>${model}</td>
        <td class="vin-cell">${esc(r.detected_vin)}</td>
        <td>${rfidCell(r)}</td>
        <td>${raw}</td>
        <td>${confBar(r.confidence)}</td>
        <td>${statusBadge(r.status)}</td>
        <td>${img}</td>
      </tr>`;
    }).join("");

    recBody.querySelectorAll(".thumb").forEach((t) =>
      t.addEventListener("click", () => openModal(t.dataset.full, t.dataset.cap)));
  }

  async function load(resetPage, signal) {
    if (resetPage) page = 1;
    try {
      const p = filterParams();
      p.set("sort_by", sortBy); p.set("order", order); p.set("limit", "150");
      const response = await fetch(`/api/records?${p.toString()}`, { signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      rows = data.records || [];
      computeStats();
      renderPage();
    } catch (e) {
      recBody.innerHTML = `<tr><td colspan="9" class="empty">Error: ${esc(String(e))}</td></tr>`;
    }
  }

  function requestLoad(resetPage) {
    resetOnNextLoad = !!resetPage;
    historyPoller.runNow();
  }

  // ---- modal ----
  function openModal(src, cap) {
    $("imgModalImg").src = src; $("imgCaption").textContent = cap || "";
    $("imgModal").classList.add("show");
  }
  $("imgClose")?.addEventListener("click", () => $("imgModal")?.classList.remove("show"));
  $("imgModal")?.addEventListener("click", (e) => { if (e.target.id === "imgModal") $("imgModal").classList.remove("show"); });

  // ---- sorting ----
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (sortBy === col) order = order === "DESC" ? "ASC" : "DESC";
      else { sortBy = col; order = "DESC"; }
      document.querySelectorAll("th[data-sort]").forEach((h) => h.textContent = h.textContent.replace(/[ ▼▲]+$/, ""));
      th.textContent += order === "DESC" ? " ▼" : " ▲";
      requestLoad(true);
    });
  });

  // ---- pagination ----
  $("pgPrev")?.addEventListener("click", () => { if (page > 1) { page--; renderPage(); } });
  $("pgNext")?.addEventListener("click", () => { page++; renderPage(); });

  // ---- filters ----
  $("btnSearch")?.addEventListener("click", () => requestLoad(true));
  ["searchVin", "searchEpc"].forEach((id) => $(id)?.addEventListener("keydown", (e) => { if (e.key === "Enter") requestLoad(true); }));
  ["filterModel", "filterScore", "dateStart", "dateEnd"].forEach((id) => $(id)?.addEventListener("change", () => requestLoad(true)));
  $("btnClearFilter")?.addEventListener("click", () => {
    ["searchVin", "searchEpc", "dateStart", "dateEnd"].forEach((id) => { if ($(id)) $(id).value = ""; });
    if ($("filterModel")) $("filterModel").value = "";
    if ($("filterScore")) $("filterScore").value = "0";
    requestLoad(true);
  });
  $("btnRefresh")?.addEventListener("click", () => requestLoad(false));
  $("btnCsv")?.addEventListener("click", () => { const p = filterParams(); p.set("fmt", "csv"); window.location = `/api/export?${p}`; });
  $("btnXlsx")?.addEventListener("click", () => { const p = filterParams(); p.set("fmt", "xlsx"); window.location = `/api/export?${p}`; });

  const historyPoller = window.AICAMPolling.create("history-records", (signal) => {
    const reset = resetOnNextLoad;
    resetOnNextLoad = false;
    if (!$("autoRefresh")?.checked && !reset) return;
    return load(reset, signal);
  }, { intervalMs: 7000 }).start();
  window.addEventListener("langchange", () => renderPage());
})();
