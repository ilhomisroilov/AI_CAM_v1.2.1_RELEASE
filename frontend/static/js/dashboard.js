/* ============================================================
   dashboard.js — status cards, live stream, PLC control, stats.
   Lightweight: writes the DOM only when a value changes, never
   touches the video <img> on a poll. Re-renders on language change.
   ============================================================ */
(() => {
  const $ = (id) => document.getElementById(id);
  const T = (k) => (window.t ? window.t(k) : k);
  const videoFeed = $("videoFeed");
  const videoPlaceholder = $("videoPlaceholder");
  const liveBadge = $("liveBadge");
  const camDot = $("camDot");

  let videoOn = false;
  let _cache = {};
  function setText(id, val) {
    const el = $(id); if (!el) return;
    val = String(val);
    if (_cache[id] === val) return;
    _cache[id] = val; el.textContent = val;
  }
  function setTileCls(tileId, cls) {
    const el = $(tileId); if (!el) return;
    const want = "tile" + (cls ? " " + cls : "");
    if (el.className !== want) el.className = want;
  }

  async function post(url, body) {
    const opts = { method: "POST" };
    if (body !== undefined) { opts.headers = { "Content-Type": "application/json" }; opts.body = JSON.stringify(body); }
    return (await fetch(url, opts)).json();
  }

  function syncVideo(camConnected) {
    if (camConnected && !videoOn) {
      videoFeed.src = "/video_feed?ts=" + Date.now();   // set ONCE on connect
      videoPlaceholder.style.display = "none";
      camDot.className = "status-dot on"; liveBadge.classList.add("on"); videoOn = true;
    } else if (!camConnected && videoOn) {
      videoFeed.src = ""; videoPlaceholder.style.display = "flex";
      camDot.className = "status-dot off"; liveBadge.classList.remove("on"); videoOn = false;
    }
  }

  async function refreshStatus(signal) {
    try {
      const s = await (await fetch("/api/status", { signal })).json();
      const st = s.stats || {}, ocr = s.ocr || {};
      setText("statFrames", st.frames ?? 0);
      setText("statDet", st.detections ?? 0);
      setText("statVin", st.vins ?? 0);
      setText("statRfid", st.rfid_reads ?? 0);
      setText("statOcr", (ocr.processed ? Math.round((ocr.accepted / ocr.processed) * 100) : 0) + "%");

      setText("vsFps", st.fps ?? 0);
      setText("vsDet", st.detections ?? 0);
      setText("vsVin", ocr.last_vin || "—");

      setText("camStateText", s.camera_connected ? T("st.online") : T("st.offline"));
      setText("tCam", s.camera_connected ? T("st.online") : T("st.offline"));
      setText("tCamSub", s.camera_connected ? `${st.fps || 0} fps` : T("st.stream_idle"));
      setTileCls("tileCam", s.camera_connected ? "ok" : "bad");

      setText("tAi", s.yolo_ready ? T("st.ready") : T("st.no_model"));
      setText("tAiSub", `OCR ${ocr.running ? T("st.running") : T("st.idle")} · ${ocr.avg_ms || 0} ms`);
      setTileCls("tileAi", s.yolo_ready ? "ok" : "warn");

      setText("tVin", ocr.last_vin || "—");
      setText("tVinSub", ocr.last_conf ? `score ${ocr.last_conf}` : T("st.awaiting"));

      syncVideo(s.camera_connected);
    } catch (e) {}
  }

  if ($("btnPlcOn")) $("btnPlcOn").addEventListener("click", () => post("/api/plc/sim", { state: 1 }).then(refreshPlc));
  if ($("btnPlcOff")) $("btnPlcOff").addEventListener("click", () => post("/api/plc/sim", { state: 0 }).then(refreshPlc));

  async function refreshPlc(signal) {
    try {
      const s = await (await fetch("/api/plc/status", { signal })).json();
      const on = s.signal === 1;
      const dotCls = "status-dot " + (on ? "on" : "off");
      if ($("plcDot").className !== dotCls) $("plcDot").className = dotCls;
      setText("plcStateText", s.signal === null ? "—" : (on ? "ON (1)" : "OFF (0)"));
      setText("plcMode", s.mode === "melsec" ? `MC ${s.connected ? T("st.connected") : T("st.disconnected")}` : T("st.simulator"));
      setText("tPlc", on ? "RUN (1)" : "IDLE (0)");
      setText("tPlcSub", `${s.mode}${s.mode === "melsec" ? " · " + (s.connected ? T("st.connected") : T("st.disconnected")) : ""}`);
      setTileCls("tilePlc", on ? "ok" : "warn");
      const sim = s.is_simulator;
      $("btnPlcOn").disabled = !sim; $("btnPlcOff").disabled = !sim;
    } catch (e) {}
  }

  async function refreshRfid(signal) {
    try {
      const s = await (await fetch("/api/rfid/status", { signal })).json();
      if (!s || s.enabled === false) { setText("tRfid", "Off"); setText("tRfidSub", T("st.disabled")); setTileCls("tileRfid", ""); return; }
      let cls = "warn";
      if (s.state === "OK") cls = "ok";
      else if (s.state === "NO_READ" || s.state === "ERROR") cls = "bad";
      else if (s.mode === "r700" && !s.connected) cls = "bad";
      const conn = s.mode === "r700" ? (s.connected ? T("st.connected") : T("st.disconnected")) : T("st.simulator");
      setText("tRfid", s.last_number || "—");
      setText("tRfidSub", `${conn} · ${s.state || "—"}`);
      setTileCls("tileRfid", cls);
    } catch (e) {}
  }

  // Re-translate dynamic values when language changes
  async function refreshAll(signal) {
    await Promise.allSettled([
      refreshStatus(signal),
      refreshPlc(signal),
      refreshRfid(signal)
    ]);
  }

  const dashboardPoller = window.AICAMPolling.create(
    "dashboard-status",
    refreshAll,
    { intervalMs: 5000 }
  ).start();

  window.addEventListener("langchange", () => {
    _cache = {};
    dashboardPoller.runNow();
  });
})();
