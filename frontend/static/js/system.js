/* ============================================================
   system.js — service health grid (derived from live APIs).
   ============================================================ */
(() => {
  const $ = (id) => document.getElementById(id);
  const getJSON = async (u) => { try { return await (await fetch(u)).json(); } catch (e) { return null; } };

  function set(id, cls, meta) {
    const el = $(id); if (!el) return;
    const dot = el.querySelector(".status-dot");
    if (dot) dot.className = "status-dot " + cls;
    const m = el.querySelector(".svc-meta");
    if (m) m.textContent = meta;
  }

  async function refresh() {
    const [st, plc, rfid] = await Promise.all([
      getJSON("/api/status"), getJSON("/api/plc/status"), getJSON("/api/rfid/status"),
    ]);

    // Log + Settings services are up if the API responded at all
    const apiUp = st !== null;
    set("svcLog", apiUp ? "on" : "bad", apiUp ? "Streaming events" : "Unreachable");
    set("svcSettings", apiUp ? "on" : "bad", apiUp ? "config/settings.yaml loaded" : "Unreachable");
    set("svcDatabase", apiUp ? "on" : "bad", apiUp ? "SQLite · auto-persist" : "Unreachable");

    if (st) {
      set("svcCamera", st.camera_connected ? "on" : "off",
          st.camera_connected ? `Online · ${(st.stats || {}).fps || 0} fps` : "Idle (PLC controlled)");
      set("svcDetection", st.yolo_ready ? "on" : "bad",
          st.yolo_ready ? "YOLOv8n loaded (memory)" : "Model not loaded");
      const ocr = st.ocr || {};
      const rate = ocr.processed ? Math.round((ocr.accepted / ocr.processed) * 100) : 0;
      set("svcOcr", ocr.running ? "on" : "warn",
          `PaddleOCR · ${ocr.running ? "running" : "idle"} · ${rate}% ok · ${ocr.avg_ms || 0}ms`);
    } else {
      ["svcCamera", "svcDetection", "svcOcr"].forEach((s) => set(s, "bad", "Unreachable"));
    }

    if (plc) {
      const on = plc.signal === 1;
      const cls = plc.mode === "melsec" ? (plc.connected ? (on ? "on" : "warn") : "bad") : (on ? "on" : "warn");
      set("svcPlc", cls, `${plc.mode} · ${plc.signal === null ? "—" : (on ? "RUN (1)" : "IDLE (0)")}`);
    } else set("svcPlc", "bad", "Unreachable");

    if (rfid) {
      if (rfid.enabled === false) set("svcRfid", "off", "Disabled (config)");
      else {
        const conn = rfid.mode === "r700" ? (rfid.connected ? "connected" : "offline") : "simulator";
        let cls = "warn";
        if (rfid.state === "OK") cls = "on";
        else if (rfid.state === "NO_READ" || rfid.state === "ERROR" || (rfid.mode === "r700" && !rfid.connected)) cls = "bad";
        set("svcRfid", cls, `${conn} · ${rfid.state || "—"}`);
      }
    } else set("svcRfid", "bad", "Unreachable");

    $("svcUpdated").textContent = "Updated " + new Date().toLocaleTimeString();
  }

  refresh();
  setInterval(refresh, 2000);
})();
