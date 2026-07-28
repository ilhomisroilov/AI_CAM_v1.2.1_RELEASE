"""
============================================================
server.py  —  FastAPI backend + MJPEG live stream + REST API
============================================================
Endpointlar:
  GET  /                      -> /dashboard ga yo'naltirish
  GET  /dashboard             -> dashboard sahifasi
  GET  /history               -> history sahifasi
  GET  /video_feed            -> MJPEG live stream (YOLO bbox bilan)
  POST /api/camera/connect    -> Connect Camera
  POST /api/processing/start  -> Start
  POST /api/processing/stop   -> Stop
  GET  /api/status            -> tizim holati + statistika
  GET  /api/logs?since=ID     -> yangi loglar (UI yon panel pollingi)
  GET  /api/records           -> history jadval ma'lumotlari (saralash)
  GET  /api/export?fmt=csv|xlsx -> eksport
  GET  /crops/{name}          -> saqlangan crop rasmlari
"""
from __future__ import annotations

import asyncio
import copy
import csv
import io
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

os.environ.setdefault("PYTORCH_NVML_BASED_CUDA_CHECK", "1")
os.environ.setdefault("YOLO_AUTOINSTALL", "false")
os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")

import torch
import ultralytics
from ultralytics.nn.tasks import DetectionModel
# PyTorch'ga YOLO modelini xavfsiz deb tanitish
torch.serialization.add_safe_globals([DetectionModel])

from fastapi import FastAPI, Form, Query
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from . import auth
from .config import AUTH, CROPS_DIR, DB_PATH, OCR, PLC, RFID, SERVER
from .database import db
from .logger import (log, ui_handler, search_files, clear_files,
                     LOG_FILES, SOURCES)
from .pipeline import pipeline
from .plc.plc_service import PLCService
from .rfid.rfid_service import RFIDService
from .runtime import (apply_safe_runtime_policy, enforce_runtime_startup,
                      get_runtime_diagnostics, log_runtime_report,
                      record_ocr_preload, runtime_health_summary)
from .production_runtime import operational_snapshot, run_network_preflight
from .version import API_VERSION, APP_VERSION

# Performance metrics uchun psutil (ixtiyoriy — bo'lmasa null qaytadi)
try:
    import psutil
    _PROC = psutil.Process()
    psutil.cpu_percent(interval=None)        # cpu_percent ni "prime" qilamiz
except Exception:
    psutil = None
    _PROC = None

# --- Papka manzillari ---
_HERE = Path(__file__).resolve().parent
_FRONTEND = _HERE.parent / "frontend"

# PLC xizmati — D2222 rising pulse bitta sessiyani ochadi; pipeline shu callback
# ichida RFID va kamerani parallel ishga tushiradi.
plc_service = PLCService(on_start=pipeline.plc_on, on_stop=pipeline.plc_off)
# ONE-SHOT: VIN o'qilgach pipeline PLC signalini 0 ga tushiradi (kamera o'chadi).
pipeline.on_vin_done = plc_service.notify_vin_done

# RFID xizmati — PLC=1 da kamera bilan PARALLEL teg o'qiydi (gateway_python uslubi)
rfid_service = RFIDService()
pipeline.rfid_service = rfid_service


# P17 FIX: @app.on_event eskirgan -> lifespan (graceful startup/shutdown).
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    auth.enforce_startup_security_policy()
    db.init_db()
    preflight = run_network_preflight()
    for endpoint, result in preflight["endpoints"].items():
        if result.get("reachable") is False:
            log.warning(
                "[PREFLIGHT] %s unreachable at %s:%s (%s)",
                endpoint, result.get("host"), result.get("port"), result.get("error"),
            )
    # Requested GPU settings are accepted only when the installed native engine
    # can actually use CUDA.  A CPU-only Windows host remains operational.
    runtime_report = apply_safe_runtime_policy(OCR)
    log_runtime_report(runtime_report, log)
    # A Linux/NVIDIA production host must not reach device/service startup with
    # a visible-but-broken CUDA/cuDNN stack. Explicit CPU fallback remains
    # available through AI_CAM_ALLOW_CPU_FALLBACK=1.
    enforce_runtime_startup(runtime_report)
    preload_ready = pipeline.warmup_models()  # PLC=1 bo'lganda kechikish bo'lmaydi
    runtime_report = record_ocr_preload(runtime_report, preload_ready)
    try:
        enforce_runtime_startup(runtime_report)
    except Exception:
        pipeline.ocr.shutdown()
        raise
    if PLC.enabled:
        plc_service.start()
    if RFID.enabled:
        rfid_service.start()
    log.info("AI_CAM server ishga tushdi.")
    try:
        yield
    finally:
        # --- Graceful shutdown (P17) ---
        from .ai.ocr_shadow_hook import shutdown_shadow_ocr

        for name, fn in (
            ("PLC", plc_service.stop),
            ("sessions", pipeline.prepare_shutdown),
            ("processing", pipeline.stop_processing),
            ("kamera", pipeline.disconnect_camera),
            ("RFID", rfid_service.stop),
            ("OCR shadow", shutdown_shadow_ocr),
            ("OCR", pipeline.ocr.shutdown),
        ):
            try:
                fn()
            except Exception as exc:
                log.warning(f"Shutdown ({name}) xatosi: {exc}")
        log.info("AI_CAM server to'xtatildi (graceful).")


app = FastAPI(title="AI_CAM — Industrial VIN Vision", version=API_VERSION, lifespan=lifespan)

# Statik fayllar va shablonlar
app.mount("/static", StaticFiles(directory=str(_FRONTEND / "static")), name="static")
templates = Jinja2Templates(directory=str(_FRONTEND / "templates"))
templates.env.globals["app_version"] = APP_VERSION


# ===================================================================
# Autentifikatsiya — barcha sahifa/API uchun himoya darvozasi (middleware)
# ===================================================================
# Login talab QILINMAYDIGAN yo'llar (login sahifasi, statik fayllar)
_PUBLIC_PATHS = {"/login", "/favicon.ico", "/health"}
# Login bo'lmasa 401 (redirect emas) qaytariladigan API/oqim yo'llari
_API_PREFIXES = ("/api", "/video_feed", "/crops")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _current_role(request: Request) -> str | None:
    return auth.get_role(request.cookies.get(AUTH.cookie_name))


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if (not AUTH.enabled) or path in _PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)

    token = request.cookies.get(AUTH.cookie_name)
    if not auth.is_valid(token):
        # API / video so'rovlari uchun 401, sahifalar uchun login ga yo'naltirish
        if path.startswith(_API_PREFIXES):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    # Allaqachon login bo'lsa — to'g'ridan-to'g'ri dashboard ga
    if auth.is_valid(request.cookies.get(AUTH.cookie_name)):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html",
                                      context={"error": None})


@app.post("/login")
async def login_post(request: Request):
    # Form ni QO'LDA o'qiymiz — python-multipart shart EMAS (urlencoded yoki JSON).
    # Shu sababdan kutubxona o'rnatilmagan bo'lsa ham server ishlaydi.
    username = password = ""
    ctype = request.headers.get("content-type", "")
    try:
        if "application/json" in ctype:
            body = await request.json()
            username = str((body or {}).get("username", ""))
            password = str((body or {}).get("password", ""))
        else:
            from urllib.parse import parse_qs
            raw = (await request.body()).decode("utf-8", "ignore")
            data = parse_qs(raw)
            username = (data.get("username") or [""])[0]
            password = (data.get("password") or [""])[0]
    except Exception as exc:
        log.warning(f"Login formani o'qishda xato: {exc}")

    identifier = _client_ip(request)
    locked, retry_after = auth.is_locked_out(identifier)
    if locked:
        log.warning(f"Login vaqtincha bloklandi: manba={identifier}")
        return JSONResponse(
            {"error": "Juda ko'p noto'g'ri urinish. Keyinroq qayta urinib ko'ring."},
            status_code=429, headers={"Retry-After": str(retry_after)})

    role = auth.check_credentials(username, password)
    if role:
        auth.record_login_success(identifier)
        token, csrf_token = auth.create_session(role=role, username=username)
        resp = RedirectResponse(url="/dashboard", status_code=303)
        resp.set_cookie(AUTH.cookie_name, token, httponly=True,
                        samesite="lax", secure=getattr(AUTH, "cookie_secure", False),
                        max_age=AUTH.session_ttl_sec)
        resp.set_cookie("ai_cam_csrf", csrf_token, httponly=False,
                        samesite="lax", secure=getattr(AUTH, "cookie_secure", False),
                        max_age=AUTH.session_ttl_sec)
        log.info(f"Login muvaffaqiyatli: {username} (rol={role})")
        return resp
    auth.record_login_failure(identifier)
    log.warning(f"Login muvaffaqiyatsiz urinish: {username}")
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"error": "Login yoki parol noto'g'ri."}, status_code=401)


@app.get("/logout")
def logout(request: Request):
    auth.destroy(request.cookies.get(AUTH.cookie_name))
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(AUTH.cookie_name)
    resp.delete_cookie("ai_cam_csrf")
    return resp


# ===================================================================
# Sahifalar
# ===================================================================
@app.get("/", response_class=RedirectResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="dashboard.html")


@app.get("/history", response_class=HTMLResponse)
def history(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="history.html")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="settings.html")


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="logs.html")


@app.get("/system", response_class=HTMLResponse)
def system_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="system.html")

# ===================================================================
# MJPEG live stream
# ===================================================================
async def _mjpeg_generator():
    """Multipart MJPEG: pipeline'dan oxirgi kadrni doimiy uzatadi."""
    boundary = b"--frame"
    interval = 1.0 / max(1, SERVER.mjpeg_fps)
    while True:
        jpeg = pipeline.get_jpeg()
        if jpeg is not None:
            yield (boundary + b"\r\n"
                   b"Content-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                   + jpeg + b"\r\n")
        await asyncio.sleep(interval)


@app.get("/video_feed")
def video_feed() -> StreamingResponse:
    return StreamingResponse(
        _mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ===================================================================
# PLC API (simulator boshqaruvi + holat)
# ===================================================================
@app.get("/api/plc/status")
def api_plc_status() -> JSONResponse:
    return JSONResponse(plc_service.status())


# ===================================================================
# RFID API (status + qo'lda sinov o'qishi)
# ===================================================================
@app.get("/api/rfid/status")
def api_rfid_status() -> JSONResponse:
    return JSONResponse(rfid_service.status())


@app.post("/api/rfid/read")
def api_rfid_read() -> JSONResponse:
    """Qo'lda RFID o'qishni ishga tushiradi (sinov uchun — kameradan mustaqil)."""
    if not RFID.enabled:
        return JSONResponse({"ok": False, "error": "RFID o'chirilgan (config)."},
                            status_code=409)
    ok = rfid_service.trigger_read(None)
    return JSONResponse({"ok": ok, "status": rfid_service.status()})


@app.post("/api/plc/sim")
async def api_plc_sim(request: Request) -> JSONResponse:
    """PLC simulyator holatini o'rnatadi (UI 'PLC ON/OFF' tugmalari). JSON: {"state":0|1}."""
    try:
        body = await request.json()
        state = int((body or {}).get("state", 0))
    except Exception:
        return JSONResponse({"ok": False, "error": "state (0/1) kerak"}, status_code=400)
    # Simulator rejimida xizmat ishlamayotgan bo'lsa, talab bo'yicha yoqamiz
    if not plc_service._running and plc_service.status().get("is_simulator"):
        plc_service.start()
    ok = plc_service.set_sim_state(state)
    if not ok:
        return JSONResponse({"ok": False, "error": "simulator rejimida emas (mode=melsec)"},
                            status_code=409)
    return JSONResponse({"ok": True, "state": state, "status": plc_service.status()})


# ===================================================================
# Boshqaruv API (Connect / Start / Stop)
# ===================================================================
@app.post("/api/camera/connect")
async def api_connect(request: Request) -> JSONResponse:
    """
    Kamera IP manzili frontend dan dinamik keladi (JSON: {"ip": "192.168.1.10"}).
    Hardcode IP ISHLATILMAYDI. BLOB ulanishi Port 2113 da amalga oshiriladi.
    """
    ip = None
    try:
        body = await request.json()
        ip = (body or {}).get("ip")
    except Exception:
        ip = None  # tana bo'sh bo'lsa — config standart IP ishlatiladi
    if not ip:
        return JSONResponse(
            {"ok": False, "error": "Camera IP kiritilmadi.", "status": pipeline.status()},
            status_code=400,
        )
    ok = pipeline.connect_camera(ip=ip)
    return JSONResponse({"ok": ok, "ip": ip, "status": pipeline.status()})


@app.post("/api/camera/disconnect")
def api_disconnect() -> JSONResponse:
    pipeline.disconnect_camera()
    return JSONResponse({"ok": True, "status": pipeline.status()})


@app.post("/api/processing/start")
def api_start() -> JSONResponse:
    ok = pipeline.start_processing()
    return JSONResponse({"ok": ok, "status": pipeline.status()})


@app.post("/api/processing/stop")
def api_stop() -> JSONResponse:
    pipeline.stop_processing()
    return JSONResponse({"ok": True, "status": pipeline.status()})


@app.get("/api/status")
def api_status() -> JSONResponse:
    return JSONResponse(pipeline.status())


def _extract_metrics(status: dict) -> dict:
    """Normalize the operator/monitor metric contract defensively."""
    status = status if isinstance(status, dict) else {}
    metrics = status.get("metrics") if isinstance(status.get("metrics"), dict) else {}
    session = status.get("session") if isinstance(status.get("session"), dict) else {}

    def count(name: str, *fallback_names: str) -> int:
        value = metrics.get(name)
        if value is None:
            for fallback in fallback_names:
                if fallback in session:
                    value = session.get(fallback)
                    break
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    age = metrics.get("camera_last_frame_age")
    if age is None:
        try:
            last_frame_ts = float(status.get("last_frame_ts") or 0)
            age = max(0.0, time.time() - last_frame_ts) if last_frame_ts else None
        except (TypeError, ValueError):
            age = None

    return {
        "active_session": bool(session.get("active", False)),
        "active_session_id": session.get("id"),
        "pending_trigger_count": count("pending_trigger_count", "pending_triggers"),
        "dropped_trigger_count": count("dropped_trigger_count", "dropped_trigger_count"),
        "session_mismatch_count": count("session_mismatch_count"),
        "late_ocr_result_count": count("late_ocr_result_count"),
        "late_rfid_result_count": count("late_rfid_result_count"),
        "duplicate_finalize_attempt_count": count("duplicate_finalize_attempt_count"),
        "camera_last_frame_age": age,
        "camera_reconnect_count": count("camera_reconnect_count"),
        "plc_ack_timeout_count": count("plc_ack_timeout_count"),
        "database_write_failure_count": count("database_write_failure_count"),
        "current_session_state": session.get("state"),
        "d2222_at": session.get("d2222_at"),
        "d2223_at": session.get("d2223_at"),
        "grace_remaining_sec": session.get("grace_remaining_sec"),
        "finalizing_session_count": count("finalizing_session_count", "finalizing_session_count"),
        "last_completed_session": session.get("last_completed"),
    }


@app.get("/api/metrics")
def api_metrics() -> JSONResponse:
    return JSONResponse(_extract_metrics(pipeline.status()))


# ===================================================================
# Healthcheck (P17) — monitoring/deploy uchun yagona yashil/qizil endpoint
# (PUBLIC: auth talab qilinmaydi — tashqi monitor uchun)
# ===================================================================
@app.get("/health")
def health() -> JSONResponse:
    """
    Tizim sog'lig'i: GPU/PLC/RFID/camera/OCR holati bitta joyda.
    HTTP 200 = sog'lom (yoki degraded), 503 = kritik komponent ishlamayapti.
    """
    pst = pipeline.status()
    plc = plc_service.status()
    rfid = rfid_service.status()

    runtime = runtime_health_summary(get_runtime_diagnostics())
    operational = operational_snapshot()
    ocr_stats = pst.get("ocr", {})

    # v1.2.1: additive engine/model/version/readiness report (never breaks /health).
    try:
        from .ai.engines import engines_health_report
        ocr_engines_report = engines_health_report()
    except Exception:
        ocr_engines_report = []
    try:
        from .ai.ocr_shadow_hook import shadow_status

        shadow = shadow_status()
    except Exception as exc:
        shadow = {"ready": False, "error": f"{type(exc).__name__}: {exc}"}

    components = {
        "yolo": {"ready": pst.get("yolo_ready", False),
                 "device": getattr(pipeline.detector, "device", "?")},
        "ocr": {"running": ocr_stats.get("running", False),
                "gpu": bool(runtime["policy"].get("ocr_gpu_effective")),
                "preload": runtime.get("ocr_preload", {}),
                "engines": ocr_engines_report,
                "shadow": shadow},
        "plc": {"enabled": PLC.enabled, "running": plc.get("running", False),
                "connected": plc.get("connected", False), "mode": plc.get("mode"),
                "signal": plc.get("signal")},
        "rfid": {"enabled": RFID.enabled, "mode": rfid.get("mode"),
                 "connected": rfid.get("connected", rfid.get("mode") == "simulator")},
        "camera": {"connected": pst.get("camera_connected", False),
                   "capture_alive": pst.get("stream", {}).get("capture_alive", False),
                   "decode_format": pst.get("stream", {}).get("decode_format"),
                   "decode_fail_total": pst.get("stream", {}).get("decode_fail_total", 0)},
        "runtime": runtime,
        "database": operational["database"],
        "disk": operational["disk"],
        "process": operational["process"],
        "network_preflight": operational["network_preflight"],
    }

    # Kritiklik: PLC yoqilgan-u ishlamayapti, yoki YOLO tayyor emas -> degraded/critical
    critical = (
        not pst.get("yolo_ready", False)
        or runtime.get("status") == "critical"
    )
    # Test clients and pre-start probes intentionally do not enter ASGI lifespan;
    # an unstarted PLC service is not a runtime failure. If its thread existed
    # and died, however, readiness must turn red.
    plc_was_started = getattr(plc_service, "_thread", None) is not None
    if PLC.enabled and plc_was_started and not plc.get("running", False):
        critical = True
    if operational["database"].get("integrity") in ("error",):
        critical = True
    if any(
        item.get("status") == "critical"
        for item in operational["disk"].values()
    ):
        critical = True
    status_str = "ok" if not critical else "critical"
    code = 200 if not critical else 503
    return JSONResponse({"status": status_str,
                         "application": {"name": "AI_CAM",
                                         "version": APP_VERSION,
                                         "api_version": API_VERSION},
                         "config_sources": operational["config_sources"],
                         "database_path": str(DB_PATH),
                         "components": components,
                         "session": pst.get("session", {}),
                         "health": pst.get("health", {}),
                         "stats": pst.get("stats", {}),
                         "stream": pst.get("stream", {})}, status_code=code)


# ===================================================================
# Real-time loglar (UI yon panel pollingi)
# ===================================================================
@app.get("/api/logs")
def api_logs(since: int = Query(0)) -> JSONResponse:
    return JSONResponse({"logs": ui_handler.get_since(since)})


# ===================================================================
# System Logs — kengaytirilgan monitoring API
# (himoya: barcha /api/* yo'llari auth middleware orqasida)
# ===================================================================
@app.get("/api/logs/recent")
def api_logs_recent(limit: int = Query(200, le=2000), offset: int = Query(0, ge=0),
                    source: str = Query(""), level: str = Query(""),
                    q: str = Query("")) -> JSONResponse:
    """Xotira bufferidan filtrlangan, sahifalangan loglar (eng yangidan)."""
    return JSONResponse(ui_handler.query(
        source=source or None, level=level or None, q=q or None,
        limit=int(limit), offset=int(offset)))


@app.get("/api/logs/search")
def api_logs_search(q: str = Query(""), source: str = Query(""), level: str = Query(""),
                    start: str = Query(""), end: str = Query(""),
                    limit: int = Query(500, le=5000)) -> JSONResponse:
    """system.log dan chuqurroq tarixiy qidiruv (sana/manba/daraja/matn)."""
    return JSONResponse({"items": search_files(
        q=q or None, source=source or None, level=level or None,
        start=start or None, end=end or None, limit=int(limit))})


@app.get("/api/logs/stream")
async def api_logs_stream() -> StreamingResponse:
    """Real-time log oqimi (Server-Sent Events) — sahifa yangilanmasdan yangilanadi."""
    async def gen():
        recent = ui_handler.snapshot()[-80:]
        last = recent[-1]["id"] if recent else ui_handler.last_id()
        yield ": connected\n\n"
        for e in recent:
            yield f"data: {json.dumps(e)}\n\n"
        while True:
            for e in ui_handler.get_since(last):
                last = e["id"]
                yield f"data: {json.dumps(e)}\n\n"
            await asyncio.sleep(0.6)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _fmt_ts(ts: float) -> dict:
    if not ts:
        return {"ts": None, "ago": None}
    return {"ts": time.strftime("%H:%M:%S", time.localtime(ts)),
            "ago": round(time.time() - ts, 1)}


@app.get("/api/logs/stats")
def api_logs_stats() -> JSONResponse:
    """Statistik hisoblagichlar + ulanish holati + ish unumdorligi metrikalari."""
    counts = ui_handler.stats()
    plc = plc_service.status()
    rfid = rfid_service.status()
    pst = pipeline.status()

    # --- Ulanish holati (last communication bilan) ---
    if plc.get("connected"):
        plc_state = "connected"
    elif plc.get("running"):
        plc_state = "reconnecting"
    else:
        plc_state = "disconnected"
    connections = {
        "plc": {"state": plc_state, "mode": plc.get("mode"),
                "signal": plc.get("signal"), "last": _fmt_ts(plc.get("last_read_ts", 0))},
        "rfid": {"state": "connected" if (rfid.get("connected") or rfid.get("mode") == "simulator") else "disconnected",
                 "mode": rfid.get("mode"), "reader_state": rfid.get("state"),
                 "last_tag": rfid.get("last_number"), "last_ts": rfid.get("last_ts"),
                 "cache_ingest_count": rfid.get("cache_ingest_count")},
        "camera": {"state": "connected" if pst.get("camera_connected") else "disconnected",
                   "last": _fmt_ts(pst.get("last_frame_ts", 0))},
    }

    # --- Ish unumdorligi ---
    stats = pst.get("stats", {})
    ocr = pst.get("ocr", {})
    perf = {
        "fps": stats.get("fps", 0),
        "ocr_ms": ocr.get("avg_ms", 0),
        "plc_poll_ms": PLC.poll_interval_ms,
        "rfid_window_ms": RFID.inventory_duration_ms,
        "cpu_percent": None, "ram_percent": None, "proc_mb": None,
    }
    if psutil is not None:
        try:
            perf["cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
            vm = psutil.virtual_memory()
            perf["ram_percent"] = round(vm.percent, 1)
            if _PROC is not None:
                perf["proc_mb"] = round(_PROC.memory_info().rss / (1024 * 1024), 1)
        except Exception:
            pass

    return JSONResponse({"counts": counts, "connections": connections, "performance": perf})


@app.get("/api/logs/download")
def api_logs_download(fmt: str = Query("txt"), source: str = Query(""),
                      level: str = Query(""), q: str = Query(""),
                      scope: str = Query("buffer")):
    """Filtrlangan loglarni TXT / CSV / JSON ko'rinishida yuklab beradi."""
    if scope == "file":
        entries = search_files(q=q or None, source=source or None,
                               level=level or None, limit=100000)
    else:
        entries = ui_handler.query(source=source or None, level=level or None,
                                   q=q or None, limit=100000)["items"]
    entries = list(reversed(entries))    # xronologik tartib
    fmt = (fmt or "txt").lower()

    if fmt == "json":
        buf = io.BytesIO(json.dumps(entries, ensure_ascii=False, indent=2).encode("utf-8"))
        return StreamingResponse(buf, media_type="application/json",
                                 headers={"Content-Disposition": "attachment; filename=logs.json"})
    if fmt == "csv":
        sbuf = io.StringIO()
        w = csv.DictWriter(sbuf, fieldnames=["tsfull", "level", "source", "msg"], extrasaction="ignore")
        w.writeheader()
        for e in entries:
            w.writerow({"tsfull": e.get("tsfull", e.get("ts", "")), "level": e.get("level", ""),
                        "source": e.get("source", ""), "msg": e.get("msg", "")})
        return StreamingResponse(io.BytesIO(sbuf.getvalue().encode("utf-8-sig")), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=logs.csv"})
    # default: txt
    lines = [f"[{e.get('tsfull', e.get('ts',''))}] [{e.get('level','')}] [{e.get('source','')}] {e.get('msg','')}"
             for e in entries]
    return StreamingResponse(io.BytesIO(("\n".join(lines)).encode("utf-8")), media_type="text/plain",
                             headers={"Content-Disposition": "attachment; filename=logs.txt"})


@app.delete("/api/logs/clear")
def api_logs_clear(request: Request) -> JSONResponse:
    """Clear logs only for an admin with a valid session-bound CSRF token."""
    role = _current_role(request)
    if AUTH.enabled and role != "admin":
        return JSONResponse(
            {"ok": False, "error": "Faqat admin tizim loglarini tozalashi mumkin."},
            status_code=403,
        )
    if not auth.validate_csrf(request):
        return JSONResponse(
            {"ok": False, "error": "CSRF token yo'q yoki noto'g'ri."},
            status_code=403,
        )
    session_token = request.cookies.get(AUTH.cookie_name)
    username = auth.get_username(session_token) or "unknown"
    counts_before = ui_handler.stats()
    client_ip = _client_ip(request)
    ui_handler.clear()
    clear_files()
    # Audit is intentionally emitted after clearing so it survives the action.
    log.warning(
        "[API] AUDIT: System logs cleared by user=%s role=%s ip=%s "
        "total_before_clear=%s",
        username, role, client_ip, counts_before.get("total", 0),
    )
    return JSONResponse({"ok": True})


@app.get("/api/logs/sources")
def api_logs_sources() -> JSONResponse:
    return JSONResponse({"sources": SOURCES, "files": list(LOG_FILES.keys())})


# ===================================================================
# History — yozuvlar va eksport
# ===================================================================
@app.get("/api/records")
def api_records(sort_by: str = "timestamp", order: str = "DESC",
                limit: int = 500,
                vin: str = Query("", description="VIN qidiruvi (qisman)"),
                epc: str = Query("", description="RFID EPC qidiruvi (qisman)"),
                model: str = Query("", description="model filtri (QY/BL7M)"),
                min_score: float = Query(0.0, description="minimal confidence"),
                start: str = Query("", description="boshlang'ich vaqt"),
                end: str = Query("", description="tugash vaqti")) -> JSONResponse:
    return JSONResponse({"records": db.get_records(
        limit=limit, order=order, sort_by=sort_by,
        vin=vin or None, epc=epc or None, model=model or None,
        min_score=(min_score or None), start=start or None, end=end or None)})


@app.get("/api/export")
def api_export(fmt: str = "csv",
               vin: str = Query(""), epc: str = Query(""), model: str = Query(""),
               min_score: float = Query(0.0),
               start: str = Query(""), end: str = Query("")):
    """Yozuvlarni CSV yoki Excel (.xlsx) ko'rinishida eksport qiladi (filtrlar bilan)."""
    records = db.get_all_records(vin=vin or None, start=start or None, end=end or None,
                                 epc=epc or None, model=model or None,
                                 min_score=(min_score or None))
    headers = ["id", "timestamp", "detected_vin", "raw_vin", "model", "rfid_epc",
               "rfid_raw", "confidence", "status", "image_path"]

    if fmt == "xlsx":
        try:
            from openpyxl import Workbook
        except Exception:
            return JSONResponse({"error": "openpyxl o'rnatilmagan"}, status_code=500)
        wb = Workbook()
        ws = wb.active
        ws.title = "VIN Records"
        ws.append([h.upper() for h in headers])
        for r in records:
            ws.append([r[h] for h in headers])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=vin_records.xlsx"},
        )

    # default: CSV
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers)
    writer.writeheader()
    writer.writerows(records)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vin_records.csv"},
    )


# ===================================================================
# Settings (config/settings.yaml o'qish/yozish) — secret-safe contract
# ===================================================================
_MASK_SENTINEL = "********"
_SECRET_FIELD_NAMES = {
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
}
_SECRET_EXTRA_PATHS = {("rfid", "username")}


def _is_secret_field(section: str, field: str) -> bool:
    return (
        (str(section).lower(), str(field).lower()) in _SECRET_EXTRA_PATHS
        or str(field).lower() in _SECRET_FIELD_NAMES
    )


def _mask_settings_dict(data: dict) -> dict:
    """Return a detached settings object with every configured secret masked."""
    masked = copy.deepcopy(data) if isinstance(data, dict) else {}
    for section, fields in masked.items():
        if not isinstance(fields, dict):
            continue
        for key in list(fields):
            if _is_secret_field(section, key) and fields.get(key):
                fields[key] = _MASK_SENTINEL
    return masked


def _unmask_settings_dict(new_data: dict, current_data: dict) -> dict:
    """Preserve on-disk secrets when the UI submits the mask placeholder."""
    unmasked = copy.deepcopy(new_data) if isinstance(new_data, dict) else {}
    current = current_data if isinstance(current_data, dict) else {}
    for section, fields in unmasked.items():
        if not isinstance(fields, dict):
            continue
        current_section = current.get(section)
        if not isinstance(current_section, dict):
            continue
        for key, value in list(fields.items()):
            if (_is_secret_field(section, key) and value == _MASK_SENTINEL
                    and key in current_section):
                fields[key] = current_section[key]
    return unmasked


_YAML_SECTION_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(#.*)?$')
_YAML_FIELD_RE = re.compile(r'^(\s+)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$')


def _secret_values(parsed: dict) -> list[str]:
    values: list[str] = []
    if not isinstance(parsed, dict):
        return values
    for section, fields in parsed.items():
        if not isinstance(fields, dict):
            continue
        for key, value in fields.items():
            if _is_secret_field(section, key) and value not in (None, ""):
                values.append(str(value))
    return sorted(set(values), key=len, reverse=True)


def _mask_raw_yaml(raw_text: str, parsed: dict) -> str:
    """Mask raw YAML while retaining ordinary formatting and comments."""
    output = []
    section = None
    for line in raw_text.splitlines():
        section_match = _YAML_SECTION_RE.match(line)
        if section_match and not line.startswith((" ", "\t")):
            section = section_match.group(1)
            output.append(line)
            continue
        field_match = _YAML_FIELD_RE.match(line)
        if field_match and section and _is_secret_field(section, field_match.group(2)):
            value_part = field_match.group(3)
            value = value_part.split("#", 1)[0].strip().strip('"\'')
            if value:
                output.append(
                    f'{field_match.group(1)}{field_match.group(2)}: '
                    f'"{_MASK_SENTINEL}"'
                )
                continue
        output.append(line)
    text = "\n".join(output) + ("\n" if raw_text.endswith("\n") else "")
    # Defense-in-depth: remove known secret values even if unusual YAML
    # formatting prevented the structural matcher from recognizing a field.
    for secret in _secret_values(parsed):
        text = text.replace(secret, _MASK_SENTINEL)
    return text


def _unmask_raw_yaml(raw_text: str, current_parsed: dict) -> str:
    """Replace submitted mask placeholders with their current stored values."""
    current = current_parsed if isinstance(current_parsed, dict) else {}
    output = []
    section = None
    for line in raw_text.splitlines():
        section_match = _YAML_SECTION_RE.match(line)
        if section_match and not line.startswith((" ", "\t")):
            section = section_match.group(1)
            output.append(line)
            continue
        field_match = _YAML_FIELD_RE.match(line)
        if field_match and section:
            key = field_match.group(2)
            submitted = field_match.group(3).split("#", 1)[0].strip().strip('"\'')
            current_section = current.get(section)
            if (_is_secret_field(section, key) and submitted == _MASK_SENTINEL
                    and isinstance(current_section, dict) and key in current_section):
                # safe_dump will normalize data submissions; raw mode preserves
                # layout while quoting the restored scalar.
                original = str(current_section[key]).replace('"', '\\"')
                output.append(f'{field_match.group(1)}{key}: "{original}"')
                continue
        output.append(line)
    return "\n".join(output) + ("\n" if raw_text.endswith("\n") else "")


@app.get("/api/settings")
def api_settings_get() -> JSONResponse:
    from . import settings_store
    parsed = settings_store.read_parsed()
    return JSONResponse({
        "raw": _mask_raw_yaml(settings_store.read_raw(), parsed),
        "data": _mask_settings_dict(parsed),
    })


@app.get("/api/settings/defaults")
def api_settings_defaults() -> JSONResponse:
    from . import settings_store
    return JSONResponse({"data": _mask_settings_dict(settings_store.defaults())})


@app.post("/api/settings")
async def api_settings_post(request: Request) -> JSONResponse:
    from . import settings_store
    from .config import apply_settings
    if AUTH.enabled and _current_role(request) != "admin":
        return JSONResponse(
            {"ok": False, "error": "Faqat admin settingsni o'zgartira oladi."},
            status_code=403,
        )
    if not auth.validate_csrf(request):
        return JSONResponse(
            {"ok": False, "error": "CSRF token yo'q yoki noto'g'ri."},
            status_code=403,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "JSON tana kerak"}, status_code=400)
    current = settings_store.read_parsed()
    try:
        if body.get("raw") is not None:
            settings_store.write_raw(
                _unmask_raw_yaml(str(body["raw"]), current)
            )
        elif isinstance(body.get("data"), dict):
            settings_store.write_data(
                _unmask_settings_dict(body["data"], current)
            )
        else:
            return JSONResponse({"ok": False, "error": "'raw' yoki 'data' kerak"}, status_code=400)
    except Exception:
        # Parser exceptions may echo the submitted YAML line, including a new
        # credential. Never return or log the raw exception text.
        log.warning("Settings YAML validation failed (details redacted).")
        return JSONResponse(
            {"ok": False, "error": "YAML sintaksisi yoki tuzilmasi noto'g'ri."},
            status_code=400,
        )
    try:
        apply_settings()   # ba'zi qiymatlar darhol qo'llanadi
    except Exception as exc:
        log.warning("apply_settings xatosi: %s", type(exc).__name__)
    log.info("Settings saqlandi (config/settings.yaml yangilandi).")
    return JSONResponse({"ok": True,
                         "note": "Saqlandi. To'liq qo'llanishi uchun ilovani qayta ishga tushiring."})


# ===================================================================
# Saqlangan crop rasmlari
# ===================================================================
_CROP_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}


def _dir_retention_scan(root: Path, max_age_days: float, max_total_mb: float,
                        active_grace_minutes: float) -> dict:
    """Plan bounded oldest-first cleanup without mutating the filesystem."""
    root = Path(root).resolve()
    now = time.time()
    grace_seconds = max(0.0, float(active_grace_minutes)) * 60.0
    max_age_seconds = max(0.0, float(max_age_days)) * 86400.0
    limit_bytes = max(0.0, float(max_total_mb)) * 1024 * 1024
    entries = []
    if root.is_dir():
        for path in root.rglob("*"):
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
                if not resolved.is_file():
                    continue
                stat = resolved.stat()
                entries.append((resolved, stat.st_mtime, stat.st_size))
            except (OSError, ValueError):
                continue
    entries.sort(key=lambda item: (item[1], str(item[0])))

    protected = {path for path, mtime, _ in entries if now - mtime < grace_seconds}
    candidates = set()
    if max_age_seconds > 0:
        candidates.update(
            path for path, mtime, _ in entries
            if path not in protected and now - mtime > max_age_seconds
        )

    remaining_bytes = sum(size for path, _, size in entries if path not in candidates)
    if limit_bytes > 0 and remaining_bytes > limit_bytes:
        for path, _, size in entries:
            if remaining_bytes <= limit_bytes:
                break
            if path in protected or path in candidates:
                continue
            candidates.add(path)
            remaining_bytes -= size

    return {
        "root": str(root),
        "candidates": [str(path) for path, _, _ in entries if path in candidates],
        "kept": [str(path) for path, _, _ in entries if path not in candidates],
        "total_files": len(entries),
        "total_bytes": sum(size for _, _, size in entries),
    }


def apply_retention(root: Path, max_age_days: float, max_total_mb: float,
                    active_grace_minutes: float, dry_run: bool = True) -> dict:
    """Apply a retention plan, re-validating every deletion stays in root."""
    resolved_root = Path(root).resolve()
    report = _dir_retention_scan(
        resolved_root, max_age_days=max_age_days, max_total_mb=max_total_mb,
        active_grace_minutes=active_grace_minutes,
    )
    report["dry_run"] = bool(dry_run)
    report["deleted"] = []
    report["errors"] = []
    if dry_run:
        return report
    for candidate in report["candidates"]:
        try:
            path = Path(candidate).resolve()
            path.relative_to(resolved_root)
            if path.is_file():
                path.unlink()
                report["deleted"].append(str(path))
        except (OSError, ValueError) as exc:
            report["errors"].append(f"{candidate}: {type(exc).__name__}: {exc}")
    return report


@app.get("/crops/{name}")
def get_crop(name: str):
    # Route faqat bitta rasm fayl nomini qabul qiladi. Windows backslash,
    # absolute/drive path, traversal va rasm bo'lmagan fayllarni qat'iy rad
    # qilamiz; resolve+relative_to symlink orqali chiqishni ham yopadi.
    if (not name or "/" in name or "\\" in name
            or Path(name).name != name or Path(name).suffix.lower() not in _CROP_EXTENSIONS):
        return JSONResponse({"error": "noto'g'ri crop nomi"}, status_code=400)
    root = Path(CROPS_DIR).resolve()
    path = (root / name).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return JSONResponse({"error": "noto'g'ri crop yo'li"}, status_code=400)
    if not path.is_file():
        return JSONResponse({"error": "topilmadi"}, status_code=404)
    return FileResponse(str(path))
