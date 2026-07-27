/* ============================================================
   logs.js — System Logs monitoring page.
   Real-time via SSE (/api/logs/stream) with polling fallback.
   Client-side source/level/text filters on the live buffer;
   date-range + Search hits the file-history API. Live statistics,
   connection monitor, performance metrics, export, clear.
   ============================================================ */
(() => {
  const $ = (id) => document.getElementById(id);
  const T = (k) => (window.t ? window.t(k) : k);
  const panel = $("logPanel");
  const CAP = 2000, DOM_CAP = 1500;

  let buffer = [], mode = "live", lastId = 0, es = null;

  // ---------- helpers ----------
  const getJSON = async (u, o) => { try { return await (await fetch(u, o)).json(); } catch (e) { return null; } };
  function toast(msg, ok) { const t = $("toast"); t.textContent = msg; t.className = "toast show " + (ok ? "ok" : "err"); setTimeout(() => (t.className = "toast"), 2400); }
  function setText(id, v) { const e = $(id); if (e && e.textContent !== String(v)) e.textContent = v; }

  function passes(e) {
    const src = $("fSource").value, lvl = $("fLevel").value, q = $("fSearch").value.toLowerCase().trim();
    if (src === "ERROR") { if (e.level !== "ERROR" && e.level !== "CRITICAL") return false; }
    else if (src && e.source !== src) return false;
    if (lvl && e.level !== lvl) return false;
    if (q && !(e.msg || "").toLowerCase().includes(q) && !(e.source || "").toLowerCase().includes(q)) return false;
    return true;
  }

  function makeRow(e) {
    const div = document.createElement("div");
    div.className = "log-line q4 " + e.level;
    div.innerHTML = `<span class="ts">${e.ts || ""}</span><span class="lvl">${e.level}</span>` +
                    `<span class="src-badge src-${e.source}">${e.source}</span><span class="msg"></span>`;
    div.querySelector(".msg").textContent = e.msg || "";
    return div;
  }

  function appendRow(e) {
    panel.appendChild(makeRow(e));
    while (panel.children.length > DOM_CAP) panel.removeChild(panel.firstChild);
    if ($("autoScroll").checked) panel.scrollTop = panel.scrollHeight;
  }

  function renderList(list) {
    panel.innerHTML = "";
    const frag = document.createDocumentFragment();
    for (const e of list) frag.appendChild(makeRow(e));
    panel.appendChild(frag);
    if ($("autoScroll").checked) panel.scrollTop = panel.scrollHeight;
  }

  function renderLive() {
    renderList(buffer.filter(passes));
    setText("logCount", buffer.length);
  }

  // ---------- live (SSE) ----------
  function onEntry(e) {
    buffer.push(e);
    if (buffer.length > CAP) buffer = buffer.slice(-CAP);
    lastId = e.id || lastId;
    if (mode === "live") { if (passes(e)) appendRow(e); setText("logCount", buffer.length); }
  }

  function startSSE() {
    if (typeof EventSource === "undefined") return startPolling();
    try {
      es = new EventSource("/api/logs/stream");
      es.onmessage = (ev) => { try { onEntry(JSON.parse(ev.data)); } catch (_) {} };
      es.onerror = () => { /* EventSource auto-reconnects */ };
    } catch (_) { startPolling(); }
  }
  function startPolling() {
    setInterval(async () => {
      const d = await getJSON("/api/logs?since=" + lastId);
      if (d && d.logs) for (const e of d.logs) onEntry(e);
    }, 1000);
  }

  // ---------- statistics / connections / performance ----------
  function connMeta(c) {
    const lbl = T("logs." + (c.state || "disconnected"));
    let extra = "";
    if (c.last && c.last.ts) extra = ` · ${T("logs.last")} ${c.last.ts}`;
    else if (c.last_ts) extra = ` · ${T("logs.last")} ${c.last_ts}`;
    return lbl + extra;
  }
  const dotCls = (s) => s === "connected" ? "on" : (s === "reconnecting" ? "warn" : "bad");

  async function pollStats() {
    const s = await getJSON("/api/logs/stats");
    if (!s) return;
    const c = s.counts || {};
    setText("stTotal", c.total || 0);
    setText("stErr", (c.ERROR || 0) + (c.CRITICAL || 0));
    setText("stWarn", c.WARNING || 0);
    setText("stPlc", c.plc_reconnect || 0);
    setText("stRfid", c.rfid_read || 0);
    setText("stOcr", c.ocr_success || 0);
    setText("stCam", c.camera_disconnect || 0);

    const cn = s.connections || {};
    if (cn.plc) { $("connPlcDot").className = "status-dot " + dotCls(cn.plc.state); setText("connPlc", connMeta(cn.plc)); }
    if (cn.rfid) { $("connRfidDot").className = "status-dot " + dotCls(cn.rfid.state); setText("connRfid", connMeta(cn.rfid)); }
    if (cn.camera) { $("connCamDot").className = "status-dot " + dotCls(cn.camera.state); setText("connCam", connMeta(cn.camera)); }

    const p = s.performance || {};
    setText("mFps", p.fps ?? 0);
    setText("mOcr", (p.ocr_ms ?? 0) + " ms");
    setText("mCpu", p.cpu_percent == null ? "—" : p.cpu_percent + "%");
    setText("mRam", p.ram_percent == null ? "—" : p.ram_percent + "%");
    setText("mPlc", (p.plc_poll_ms ?? 0) + " ms");
    setText("mRfid", (p.rfid_window_ms ?? 0) + " ms");
  }

  // ---------- filters / modes ----------
  function goLive() {
    mode = "live";
    $("liveDot").className = "live-dot";
    setText("liveLabel", T("logs.live"));
    renderLive();
  }
  async function doSearch() {
    const p = new URLSearchParams();
    const src = $("fSource").value, lvl = $("fLevel").value, q = $("fSearch").value.trim();
    const s = ($("fStart").value || "").replace("T", " "), e = ($("fEnd").value || "").replace("T", " ");
    if (src) p.set("source", src);
    if (lvl) p.set("level", lvl);
    if (q) p.set("q", q);
    if (s) p.set("start", s);
    if (e) p.set("end", e);
    const d = await getJSON("/api/logs/search?" + p.toString());
    mode = "search";
    $("liveDot").className = "live-dot paused";
    setText("liveLabel", T("logs.paused"));
    const items = (d && d.items) || [];
    renderList(items);
    setText("logCount", items.length);
  }

  ["fSource", "fLevel"].forEach((id) => $(id).addEventListener("change", () => { if (mode === "live") renderLive(); }));
  $("fSearch").addEventListener("input", () => { if (mode === "live") renderLive(); });
  $("fSearch").addEventListener("keydown", (ev) => { if (ev.key === "Enter") doSearch(); });
  $("btnSearch").addEventListener("click", doSearch);
  $("btnLive").addEventListener("click", goLive);

  // ---------- export ----------
  document.querySelectorAll(".dl").forEach((b) => b.addEventListener("click", () => {
    const p = new URLSearchParams();
    p.set("fmt", b.dataset.fmt);
    if ($("fSource").value) p.set("source", $("fSource").value);
    if ($("fLevel").value) p.set("level", $("fLevel").value);
    const q = $("fSearch").value.trim(); if (q) p.set("q", q);
    p.set("scope", mode === "search" ? "file" : "buffer");
    window.location = "/api/logs/download?" + p.toString();
  }));

  // ---------- clear ----------
  $("btnClear").addEventListener("click", async () => {
    if (!confirm(T("logs.clear_confirm"))) return;
    const r = await fetch("/api/logs/clear", { method: "DELETE" });
    if (r.ok) { buffer = []; renderLive(); toast(T("logs.cleared"), true); }
    else toast("Failed", false);
  });

  window.addEventListener("langchange", () => { pollStats(); });

  // ---------- boot ----------
  (async () => {
    const d = await getJSON("/api/logs/recent?limit=300");
    if (d && d.items) { buffer = d.items.slice().reverse(); lastId = buffer.length ? buffer[buffer.length - 1].id : 0; }
    renderLive();
    startSSE();
    pollStats();
    setInterval(pollStats, 2500);
  })();
})();
