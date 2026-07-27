/* ============================================================
   i18n.js — English / Uzbek translations + language switcher.
   Usage:
     - Static:  <span data-i18n="nav.dashboard">Dashboard</span>
     - Placeholder: <input data-i18n-ph="hist.vin_search">
     - HTML:    <div data-i18n-html="dash.cam_off_hint"></div>
     - Version: <span data-version></span> (server-rendered release version)
     - Dynamic JS: window.t("st.online")
   Persisted in localStorage; fires "langchange" so JS can re-render.
   ============================================================ */
(() => {
  const KEY = "ai_cam_lang";
  const APP_VERSION = document.documentElement.dataset.appVersion || "unknown";
  window.APP_VERSION = APP_VERSION;

  const DICT = {
    en: {
      "nav.dashboard": "Dashboard", "nav.history": "History", "nav.settings": "Settings", "nav.logout": "Logout",
      "theme.light": "Light", "theme.dark": "Dark", "theme.glass": "Liquid Glass",
      "common.about": "About", "common.version": "Version", "common.language": "Language",
      "common.save": "Save", "common.search": "Search", "common.clear": "Clear", "common.refresh": "Refresh",
      "common.close": "Close", "common.export_csv": "Export CSV", "common.export_excel": "Export Excel",
      "common.creator": "Created by Ilhom Isroilov",
      "brand.sub": "VIN Vision",
      "dash.sub": "Real-time VIN recognition & line monitoring",
      "dash.camera": "Camera", "dash.plc": "PLC", "dash.rfid": "RFID", "dash.ai": "AI Status", "dash.lastvin": "Last VIN",
      "dash.livestream": "Live Stream — YOLOv8n VIN Detection", "dash.plc_control": "PLC Control",
      "dash.cam_off_hint": "Camera is off.<br>Press “PLC ON (1)” or wait for a PLC signal.",
      "dash.frames": "Frames Processed", "dash.detections": "Detections", "dash.vin_saved": "VIN Saved",
      "dash.rfid_reads": "RFID Reads", "dash.ocr_success": "OCR Success",
      "hist.title": "History", "hist.sub": "Detected VIN & RFID records",
      "hist.search_filter": "Search & Filter", "hist.vin_search": "VIN search", "hist.epc_search": "EPC search",
      "hist.model": "Model", "hist.min_score": "Min score", "hist.from": "From", "hist.to": "To",
      "hist.all": "All", "hist.any": "Any", "hist.showing": "Showing", "hist.of": "of",
      "hist.total": "Total Records", "hist.today": "Today's Records", "hist.rfid_matches": "RFID Matches", "hist.vin_rate": "VIN Success Rate",
      "hist.c_time": "Timestamp", "hist.c_model": "Model", "hist.c_vin": "VIN (validated)", "hist.c_epc": "RFID EPC",
      "hist.c_raw": "Raw OCR", "hist.c_score": "Score", "hist.c_status": "Status", "hist.c_image": "Image",
      "hist.loading": "Loading…", "hist.none": "No matching records.", "hist.prev": "Prev", "hist.next": "Next", "hist.page": "Page", "hist.auto": "Auto-refresh",
      "nav.logs": "System Logs",
      "logs.title": "System Logs", "logs.sub": "Real-time monitoring of all system activity",
      "logs.stats": "Statistics", "logs.total": "Total", "logs.errors": "Errors", "logs.warnings": "Warnings",
      "logs.plc_reconnect": "PLC Reconnects", "logs.rfid_read": "RFID Reads", "logs.ocr_success": "OCR Success", "logs.cam_disconnect": "Camera Disconnects",
      "logs.connections": "Connections", "logs.performance": "Performance",
      "logs.cpu": "CPU", "logs.ram": "RAM", "logs.fps": "FPS", "logs.ocr_ms": "OCR time", "logs.plc_interval": "PLC poll", "logs.rfid_interval": "RFID window",
      "logs.source": "Source", "logs.level": "Level", "logs.all_sources": "All sources", "logs.all_levels": "All levels",
      "logs.search_ph": "Filter text…", "logs.autoscroll": "Auto-scroll", "logs.clear": "Clear", "logs.download": "Download",
      "logs.last": "Last", "logs.connected": "Connected", "logs.disconnected": "Disconnected", "logs.reconnecting": "Reconnecting",
      "logs.live": "LIVE", "logs.paused": "Paused", "logs.cleared": "Logs cleared", "logs.clear_confirm": "Clear all logs (buffer and current files)?",
      "set.title": "Settings", "set.sub": "Edit configuration (config/settings.yaml)", "set.config": "Configuration",
      "set.form": "Form", "set.raw_yaml": "Raw YAML", "set.save_config": "Save Configuration", "set.restore_defaults": "Restore Defaults",
      "set.reload": "Reload", "set.validate": "Validate", "set.save_yaml": "Save YAML",
      "set.camera": "Camera Settings", "set.plc": "PLC Settings", "set.rfid": "RFID Settings", "set.ai": "AI Settings",
      "set.database": "Database Settings", "set.system": "System Settings", "set.theme": "Theme", "set.language": "Language", "set.version": "Version",
      "set.note": "Saving updates config/settings.yaml. Some values apply immediately; model/camera/server changes require an app restart.",
      "set.db_note": "Database path is fixed in config.py. Records auto-persist on every PLC event.",
      "about.title": "About AI_CAM", "about.desc": "Industrial VIN recognition with camera OCR, RFID and PLC integration.", "about.dev": "Creator",
      "about.arch": "Architecture", "about.langs": "Languages: English · Uzbek",
      "login.signin": "Sign in to continue", "login.username": "Username", "login.password": "Password",
      "login.btn": "Sign in", "login.hint": "Authorized personnel only · Automotive",
      "st.online": "Online", "st.offline": "Offline", "st.ready": "Ready", "st.no_model": "No model",
      "st.connected": "connected", "st.disconnected": "offline", "st.simulator": "simulator",
      "st.running": "running", "st.idle": "idle", "st.awaiting": "awaiting", "st.stream_idle": "stream idle", "st.disabled": "disabled",
    },
    uz: {
      "nav.dashboard": "Boshqaruv paneli", "nav.history": "Tarix", "nav.settings": "Sozlamalar", "nav.logout": "Chiqish",
      "theme.light": "Yorug'", "theme.dark": "Tungi", "theme.glass": "Shisha",
      "common.about": "Dastur haqida", "common.version": "Versiya", "common.language": "Til",
      "common.save": "Saqlash", "common.search": "Qidirish", "common.clear": "Tozalash", "common.refresh": "Yangilash",
      "common.close": "Yopish", "common.export_csv": "CSV eksport", "common.export_excel": "Excel eksport",
      "common.creator": "Yaratuvchi: Ilhom Isroilov",
      "brand.sub": "VIN Vision",
      "dash.sub": "Real vaqtda VIN aniqlash va liniya monitoringi",
      "dash.camera": "Kamera", "dash.plc": "PLC", "dash.rfid": "RFID", "dash.ai": "AI holati", "dash.lastvin": "Oxirgi VIN",
      "dash.livestream": "Jonli oqim — YOLOv8n VIN aniqlash", "dash.plc_control": "PLC boshqaruvi",
      "dash.cam_off_hint": "Kamera o'chiq.<br>“PLC ON (1)” tugmasini bosing yoki PLC signalini kuting.",
      "dash.frames": "Qayta ishlangan kadrlar", "dash.detections": "Aniqlashlar", "dash.vin_saved": "Saqlangan VIN",
      "dash.rfid_reads": "RFID o'qishlar", "dash.ocr_success": "OCR muvaffaqiyati",
      "hist.title": "Tarix", "hist.sub": "Aniqlangan VIN va RFID yozuvlari",
      "hist.search_filter": "Qidiruv va filtr", "hist.vin_search": "VIN qidiruv", "hist.epc_search": "EPC qidiruv",
      "hist.model": "Model", "hist.min_score": "Min ishonch", "hist.from": "Dan", "hist.to": "Gacha",
      "hist.all": "Barchasi", "hist.any": "Har qanday", "hist.showing": "Ko'rsatilmoqda", "hist.of": "/",
      "hist.total": "Jami yozuvlar", "hist.today": "Bugungi yozuvlar", "hist.rfid_matches": "RFID mosliklari", "hist.vin_rate": "VIN muvaffaqiyati",
      "hist.c_time": "Vaqt", "hist.c_model": "Model", "hist.c_vin": "VIN (tasdiqlangan)", "hist.c_epc": "RFID EPC",
      "hist.c_raw": "Xom OCR", "hist.c_score": "Ishonch", "hist.c_status": "Holat", "hist.c_image": "Rasm",
      "hist.loading": "Yuklanmoqda…", "hist.none": "Mos yozuv topilmadi.", "hist.prev": "Oldingi", "hist.next": "Keyingi", "hist.page": "Sahifa", "hist.auto": "Avto-yangilash",
      "nav.logs": "Tizim loglari",
      "logs.title": "Tizim loglari", "logs.sub": "Barcha tizim faoliyatini real vaqtda kuzatish",
      "logs.stats": "Statistika", "logs.total": "Jami", "logs.errors": "Xatolar", "logs.warnings": "Ogohlantirishlar",
      "logs.plc_reconnect": "PLC qayta ulanish", "logs.rfid_read": "RFID o'qishlar", "logs.ocr_success": "OCR muvaffaqiyat", "logs.cam_disconnect": "Kamera uzilishlari",
      "logs.connections": "Ulanishlar", "logs.performance": "Unumdorlik",
      "logs.cpu": "CPU", "logs.ram": "RAM", "logs.fps": "FPS", "logs.ocr_ms": "OCR vaqti", "logs.plc_interval": "PLC so'rov", "logs.rfid_interval": "RFID oynasi",
      "logs.source": "Manba", "logs.level": "Daraja", "logs.all_sources": "Barcha manbalar", "logs.all_levels": "Barcha darajalar",
      "logs.search_ph": "Matn bo'yicha…", "logs.autoscroll": "Avto-aylantirish", "logs.clear": "Tozalash", "logs.download": "Yuklab olish",
      "logs.last": "Oxirgi", "logs.connected": "Ulangan", "logs.disconnected": "Uzilgan", "logs.reconnecting": "Qayta ulanmoqda",
      "logs.live": "JONLI", "logs.paused": "To'xtatilgan", "logs.cleared": "Loglar tozalandi", "logs.clear_confirm": "Barcha loglar tozalansinmi (bufer va joriy fayllar)?",
      "set.title": "Sozlamalar", "set.sub": "Konfiguratsiyani tahrirlash (config/settings.yaml)", "set.config": "Konfiguratsiya",
      "set.form": "Forma", "set.raw_yaml": "Xom YAML", "set.save_config": "Konfiguratsiyani saqlash", "set.restore_defaults": "Standartni tiklash",
      "set.reload": "Qayta yuklash", "set.validate": "Tekshirish", "set.save_yaml": "YAML saqlash",
      "set.camera": "Kamera sozlamalari", "set.plc": "PLC sozlamalari", "set.rfid": "RFID sozlamalari", "set.ai": "AI sozlamalari",
      "set.database": "Ma'lumotlar bazasi", "set.system": "Tizim sozlamalari", "set.theme": "Mavzu", "set.language": "Til", "set.version": "Versiya",
      "set.note": "Saqlash config/settings.yaml ni yangilaydi. Ba'zi qiymatlar darhol qo'llanadi; model/kamera/server o'zgarishlari ilovani qayta ishga tushirishni talab qiladi.",
      "set.db_note": "Baza yo'li config.py da belgilangan. Yozuvlar har PLC hodisasida avtomatik saqlanadi.",
      "about.title": "AI_CAM haqida", "about.desc": "Kamera OCR, RFID va PLC integratsiyasi bilan sanoat VIN aniqlash tizimi.", "about.dev": "Yaratuvchi",
      "about.arch": "Arxitektura", "about.langs": "Tillar: Ingliz · O'zbek",
      "login.signin": "Davom etish uchun kiring", "login.username": "Foydalanuvchi", "login.password": "Parol",
      "login.btn": "Kirish", "login.hint": "Faqat vakolatli xodimlar uchun · Avto",
      "st.online": "Onlayn", "st.offline": "Oflayn", "st.ready": "Tayyor", "st.no_model": "Model yo'q",
      "st.connected": "ulangan", "st.disconnected": "oflayn", "st.simulator": "simulyator",
      "st.running": "ishlamoqda", "st.idle": "bo'sh", "st.awaiting": "kutilmoqda", "st.stream_idle": "oqim bo'sh", "st.disabled": "o'chirilgan",
    },
  };

  function lang() { const l = localStorage.getItem(KEY) || "en"; return DICT[l] ? l : "en"; }
  window.t = (k) => { const L = lang(); return (DICT[L] && DICT[L][k]) || DICT.en[k] || k; };
  window.currentLang = lang;

  function apply() {
    const L = lang();
    document.documentElement.setAttribute("lang", L);
    document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = window.t(el.dataset.i18n); });
    document.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.setAttribute("placeholder", window.t(el.dataset.i18nPh)); });
    document.querySelectorAll("[data-i18n-html]").forEach((el) => { el.innerHTML = window.t(el.dataset.i18nHtml); });
    document.querySelectorAll("[data-version]").forEach((el) => {
      if (el instanceof HTMLInputElement) el.value = APP_VERSION;
      else el.textContent = APP_VERSION;
    });
    document.querySelectorAll(".lang-seg button[data-lang]").forEach((b) => b.classList.toggle("active", b.dataset.lang === L));
  }

  window.setLang = function (l) {
    if (!DICT[l]) l = "en";
    localStorage.setItem(KEY, l);
    apply();
    window.dispatchEvent(new CustomEvent("langchange", { detail: { lang: l } }));
  };

  // Apply ASAP and again on DOM ready (covers elements added before/after)
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.querySelectorAll(".lang-seg button[data-lang]").forEach((b) =>
      b.addEventListener("click", () => window.setLang(b.dataset.lang)));
  });
})();
