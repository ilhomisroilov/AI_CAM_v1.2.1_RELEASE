"""
============================================================
tests/conftest.py — umumiy fixture'lar (Remediation Agent 3)
============================================================
MUHIM xavfsizlik qoidalari (barcha testlar uchun):
  * HECH QANDAY real qurilmaga ulanish YO'Q (PLC/kamera/RFID). Shu sababli
    FastAPI TestClient HAR DOIM "with" kontekst menejerisiz ishlatiladi —
    bu holda Starlette/FastAPI lifespan("startup") hodisasi ISHGA TUSHMAYDI,
    demak plc_service.start()/rfid_service.start() (ular real IP larga
    ulanishga urinadi) chaqirilmaydi. Faqat auth.enforce_startup_security_policy()
    kabi TOZA (network'siz) funksiyalarni sinash uchun to'g'ridan-to'g'ri
    chaqiramiz — hech qachon to'liq ASGI lifespan orqali emas.
  * data/ai_cam.db ga YOZILMAYDI — DB kerak bo'lgan testlar yo'q (bu agent
    auth/settings/log/observability/retention bilan shug'ullanadi).
  * config/settings.yaml ga YOZILMAYDI — settings testlari
    backend.config.SETTINGS_PATH ni tmp_path ga monkeypatch qiladi.
  * data/crops va dataset/collected_raw ga TEGILMAYDI — path-traversal va
    retention testlari backend.server.CROPS_DIR / tmp_path parametrlarini
    monkeypatch qiladi.
"""
from __future__ import annotations

import logging.handlers
import os
import sys
import time
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ============================================================
# (Remediation Agent 2 qo'shimchasi) HECH QANDAY test `logs/*.log`
# fayllariga YOZMASLIGI kerak (xavfsizlik qoidasi — oldingi agent testi
# haqiqiy `DELETE /api/logs/clear` ni chaqirib real log fayllarni yo'q qilgan
# edi; bu xil oqibatlarning oldini olish uchun endi PASSIV log.info/
# warning/error yozuvlari ham diskka TEGMAYDI).
#
# MUHIM: bu — modul DARAJASIDA, `backend.server`/`backend.pipeline` import
# qilinishidan OLDIN bajariladi. Sabab: `backend.pipeline` import qilinganda
# modul darajasidagi `pipeline = Pipeline()` DARHOL ishga tushadi (YOLO model
# yuklash logi kabi) — bu COLLECTION vaqtida, har qanday pytest fixture
# ishga tushishidan OLDIN sodir bo'ladi, shuning uchun handler olib tashlash
# FIXTURE ichida bo'lsa allaqachon kech qoladi. `backend/logger.py` ning
# O'ZIGA TEGILMAYDI — faqat unga ulangan `RotatingFileHandler` lar (haqiqiy
# diskka yozadigan qism) butun test sessiyasi uchun olib tashlanadi
# (RingBuffer/UI handler va console handler tegilmaydi — ular diskka
# yozmaydi, testlar ularga bemalol tayanishi mumkin).
# ============================================================
from backend.logger import log as _app_log


def pytest_configure(config):
    """Custom markerlarni ro'yxatdan o'tkazadi (PytestUnknownMarkWarning oldini oladi)."""
    config.addinivalue_line(
        "markers", "slow: uzoq davom etuvchi test (accelerated soak va h.k.)")
    config.addinivalue_line(
        "markers", "real_ocr: HAQIQIY PaddleOCR ni alohida processda yuklaydigan "
                   "integration test (sekin — model load; offline crop, tarmoqsiz)")


_REMOVED_FILE_HANDLERS = [
    h for h in _app_log.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
]
for _h in _REMOVED_FILE_HANDLERS:
    _app_log.removeHandler(_h)

from fastapi.testclient import TestClient

from backend import auth as auth_mod
from backend import config as cfg
from backend.database import db
from backend.server import app


@pytest.fixture(autouse=True)
def _reset_auth_state():
    """Har testdan oldin/keyin auth modulining global holatini tozalaydi
    (sessiyalar, rate-limit hisoblagichlari) — testlar bir-biriga ta'sir qilmasin."""
    with auth_mod._lock:
        auth_mod._sessions.clear()
    auth_mod.reset_rate_limit_state()
    yield
    with auth_mod._lock:
        auth_mod._sessions.clear()
    auth_mod.reset_rate_limit_state()


@pytest.fixture
def client():
    """
    TestClient — ATAYLAB "with" siz (lifespan/startup ISHGA TUSHMAYDI).
    Shunday qilib PLC/RFID xizmatlari hech qachon ishga tushmaydi va
    tarmoqqa hech qanday so'rov yubormaydi.
    """
    return TestClient(app)


@pytest.fixture
def tmp_settings_yaml(tmp_path, monkeypatch):
    """
    backend.config.SETTINGS_PATH ni vaqtinchalik faylga yo'naltiradi — settings
    o'qish/yozish testlari HAQIQIY config/settings.yaml ga HECH QACHON tegmaydi.
    Ma'lum (soxta) maxfiy qiymatlar bilan minimal YAML qaytaradi.
    """
    import backend.config as config_mod

    p = tmp_path / "settings.yaml"
    p.write_text(
        "camera:\n"
        '  ip: "192.168.9.9"\n'
        '  password: "CamSecret123"   # kamera paroli\n'
        "rfid:\n"
        '  username: "root"\n'
        '  password: "RfidSecret456"\n'
        "auth:\n"
        '  password: "AuthSecret789"\n'
        "server:\n"
        "  port: 9999\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "SETTINGS_PATH", p)
    return p


def login(client: TestClient, username: str, password: str):
    # follow_redirects=False: /login muvaffaqiyatli bo'lsa 303 -> /dashboard ga
    # yo'naltiradi. Testlar login javobining O'ZINI (status/cookie) tekshiradi —
    # redirect avtomatik ergashilsa status har doim 200 (/dashboard) bo'lib,
    # login natijasini yashirib qo'yardi.
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


def get_csrf_token(client: TestClient) -> str:
    return client.cookies.get("ai_cam_csrf", "")


# ============================================================
# Quyidagi fixture'lar — Remediation Agent 1 (Session/Concurrency/DB Integrity)
# ============================================================
# MUHIM: hech qanday test HAQIQIY qurilmaga (PLC 10.123.40.99, kamera
# 10.123.86.42, RFID 10.123.90.172) ULANMAYDI va `data/ai_cam.db` ga
# YOZMAYDI. Kamera/OCR-thread ishga tushirishlari FAKE/no-op qilib
# almashtiriladi (monkeypatch) — faqat sessiya HOLAT MASHINASI (trigger
# queue, egalik/ownership, idempotentlik, dedup, DB) test qilinadi.


@pytest.fixture
def tmp_db(tmp_path):
    """Har bir test uchun ALOHIDA, vaqtinchalik SQLite DB — production DB HECH QACHON ishtirok etmaydi."""
    db_path = tmp_path / "test_ai_cam.db"
    db.set_db_path(db_path)
    db.init_db()
    yield db_path
    db.set_db_path(None)


@pytest.fixture
def pipeline_instance(tmp_db, monkeypatch):
    """
    Yangi Pipeline — kamera ulanishi/OCR-thread ishga tushishi FAKE qilinadi
    (hech qanday tarmoq so'rovi yo'q, hech qanday real qurilma ULANMAYDI).
    Standart holatda RFID.enabled=False (faqat VIN kifoya) — RFID'ga oid
    testlar buni o'zi qayta yoqadi va FakeRFIDService biriktiradi.
    """
    from backend.pipeline import Pipeline
    monkeypatch.setattr(cfg.RFID, "enabled", False)
    # v1.3.0: the harness simulates rapid multi-body sequences (triggers ~0s
    # apart), i.e. the opt-in SIMULATOR/TEST queue mode. Production (real
    # settings.yaml) ignores duplicate/early triggers during an active cycle;
    # the dedicated reliability tests (test_session_reliability_v13) opt back out.
    monkeypatch.setattr(cfg.SESSION, "pending_trigger_queue_enabled", True)
    monkeypatch.setattr(cfg.SESSION, "minimum_body_interval_sec", 0.0)
    p = Pipeline()
    monkeypatch.setattr(p, "connect_camera", lambda ip=None: True)
    monkeypatch.setattr(p, "disconnect_camera", lambda: None)
    monkeypatch.setattr(p, "start_processing", lambda: True)
    monkeypatch.setattr(p, "stop_processing", lambda: None)
    p._camera_connected = True
    p.on_vin_done = None
    yield p
    # --- Teardown: tugallanmagan sessiyalarni CANCELLED qilamiz -> ularning
    # watchdog thread'lari keyingi 0.25s siklda DB'ga YOZMASDAN chiqadi
    # (dangling finalize test izolyatsiyasini buzmasin / default DB'ga tegmasin).
    from backend.pipeline import _TERMINAL_SESSION_STATES, SessionState
    with p._session_lock:
        for s in list(p._sessions.values()):
            if s.state not in _TERMINAL_SESSION_STATES:
                s.state = SessionState.CANCELLED
                s.done_event.set()
                s.stop_event.set()


class FakeRFIDService:
    """
    Haqiqiy R700/simulyator O'RNIGA — tarmoqqa HECH QACHON ULANMAYDI.
    `behavior` orqali deterministik natija beradi (test to'liq nazorat qiladi):
      "tag"   -> darhol muvaffaqiyatli teg (session_id bilan on_done chaqiriladi)
      "no_tag"-> darhol NO_TAG
      "busy"  -> trigger_read() DARHOL False qaytaradi (on_done chaqirilmaydi) —
                 P1 audit topilmasi: chaqiruvchi buni albatta tekshirishi kerak.
    """

    def __init__(self, behavior: str = "tag", epc: str = "000000000001234", number: str = "1234"):
        self.behavior = behavior
        self.epc = epc
        self.number = number
        self.calls: list = []

    def trigger_read(self, on_done=None, stop_event=None, deadline=None,
                     session_id=None, started_at=None) -> bool:
        self.calls.append(session_id)
        if self.behavior == "busy":
            return False
        from backend.rfid.rfid_service import RFIDResult
        now = datetime.now()
        if self.behavior == "no_tag":
            result = RFIDResult(session_id=session_id, epc="", number="NO_TAG",
                                antenna=None, rssi=None, first_seen_at=now, received_at=now)
        else:
            result = RFIDResult(session_id=session_id, epc=self.epc, number=self.number,
                                antenna=1, rssi=-42.0, first_seen_at=now, received_at=now)
        if on_done is not None:
            on_done(result)
        return True

    def status(self) -> dict:
        return {"enabled": True, "mode": "fake", "connected": True, "state": "IDLE"}


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Predicate True bo'lguncha (yoki timeout) kutadi — thread/watchdog testlari uchun."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def make_ocr_result(session_id, vin="NSTFA814ATJ123456", frame_id=1, confidence=0.9,
                    model="QY", attempt_count=1, crop=None, raw_vin=None):
    """Test uchun qulay OCRResult quruvchisi (haqiqiy OCR engine ISHLATILMAYDI)."""
    from backend.ai.ocr_worker import OCRResult
    return OCRResult(
        session_id=session_id, frame_id=frame_id, vin=vin, confidence=confidence,
        model=model, completed_at=time.time(), attempt_count=attempt_count,
        raw_vin=raw_vin or vin, crop=crop,
    )
