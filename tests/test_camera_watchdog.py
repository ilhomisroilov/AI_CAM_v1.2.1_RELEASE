"""
============================================================
tests/test_camera_watchdog.py — P0-4 audit fix: kamera thread o'limi
============================================================
Audit topilmasi: `connect_camera()` thread `.start()` dan keyin SHARTSIZ
`_camera_connected = True` qilardi (stream haqiqatan ishlayotgani
TASDIQLANMASDAN). `_stream_loop` 5 ketma-ket timeout/xato bilan JIM tugar
edi — `_camera_connected` False QILINMAS, thread reset qilinmas, avtomatik
reconnect YO'Q edi. Natijada keyingi `plc_on()` kamerani qayta ulamas edi va
barcha sessiyalar kamerasiz TIMEOUT bo'lardi (operator "ishlayapti" deb ko'rardi).

Bu fayl fundamental holat-mashinasi xatti-harakatini tekshiradi (thread
exception -> state, stale frame -> health degraded, connected=true FAQAT
fresh frame kelgandan keyin, decode-error threshold -> reconnect). Reconnect
loop/backoff/shutdown/double-thread testlari alohida faylda:
tests/test_camera_reconnect.py.

Hech qanday real qurilmaga (kamera 10.123.86.42) ULANISH YO'Q — Lector652Client
FakeCameraClient bilan almashtiriladi (monkeypatch), tarmoq so'rovi umuman yo'q.
"""
from __future__ import annotations

import queue
import time

import numpy as np
import pytest

import backend.config as cfg
import backend.pipeline as pipeline_mod
from backend.pipeline import CameraState, Pipeline

from conftest import wait_until


class FakeCameraClient:
    """Lector652Client o'rniga — HECH QANDAY tarmoq so'rovi yo'q. Test har bir
    stsenariyni `frame_queue` orqali to'liq nazorat qiladi (queue.Empty ->
    TimeoutError, Exception instansi -> o'sha xato ko'tariladi, aks holda
    bayt qatori "frame" sifatida qaytariladi)."""

    def __init__(self, ip=None, control_port=None, blob_port=None, password=None, on_log=None):
        self.ip = ip
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.start_stream_calls = 0
        self.stop_stream_calls = 0
        self.connect_should_fail = False
        self.start_stream_should_fail = False
        self.frame_queue: "queue.Queue" = queue.Queue()

    def connect(self):
        self.connect_calls += 1
        if self.connect_should_fail:
            raise ConnectionError("fake connect fail (test)")

    def disconnect(self):
        self.disconnect_calls += 1

    def start_stream(self):
        self.start_stream_calls += 1
        if self.start_stream_should_fail:
            raise RuntimeError("fake start_stream fail (test)")

    def stop_stream(self):
        self.stop_stream_calls += 1

    def read_stream_frame(self):
        try:
            item = self.frame_queue.get(timeout=0.2)
        except queue.Empty:
            raise TimeoutError("fake timeout (test)")
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_client_cls(monkeypatch):
    """backend.pipeline.Lector652Client ni FakeCameraClient bilan almashtiradi."""
    monkeypatch.setattr(pipeline_mod, "Lector652Client", FakeCameraClient)
    return FakeCameraClient


@pytest.fixture
def fake_decode_ok(monkeypatch):
    """decode_bmp ni doim muvaffaqiyatli (kichik dummy grayscale kadr) qiladi —
    haqiqiy BLOB/BMP formatlashni qayta yaratish shart emas (decode_bmp — sof
    hisoblash funksiyasi, tarmoq/fayl bilan bog'liq emas)."""
    monkeypatch.setattr(pipeline_mod, "decode_bmp", lambda payload: np.zeros((8, 8), dtype=np.uint8))


@pytest.fixture
def raw_pipeline(monkeypatch):
    """Pipeline — connect_camera/disconnect_camera MONKEYPATCH QILINMAGAN
    (bu fayl aynan ularning HAQIQIY xatti-harakatini sinaydi). RFID o'chirilgan,
    hech qanday DB/tarmoq bilan ishlamaymiz (bu testlar pipeline.status()['camera']
    va connect/disconnect chaqiruvlarigagina tegishli)."""
    monkeypatch.setattr(cfg.RFID, "enabled", False)
    monkeypatch.setattr(cfg.CAMERA, "stale_after_sec", 0.2)
    monkeypatch.setattr(cfg.CAMERA, "max_consecutive_timeouts", 3)
    monkeypatch.setattr(cfg.CAMERA, "reconnect_initial_delay_sec", 0.05)
    monkeypatch.setattr(cfg.CAMERA, "reconnect_max_delay_sec", 0.2)
    monkeypatch.setattr(cfg.CAMERA, "reconnect_max_attempts", 0)
    p = Pipeline()
    yield p
    # Tozalash — fon threadlarni (agar bor bo'lsa) to'xtatamiz.
    try:
        p.disconnect_camera()
    except Exception:
        pass


# ============================================================
# 15) connected=true FAQAT fresh frame kelgandan keyin
# ============================================================
def test_camera_connected_true_only_after_fresh_frame(raw_pipeline, fake_client_cls, fake_decode_ok):
    p = raw_pipeline
    ok = p.connect_camera("10.0.0.9")
    assert ok is True
    # Thread ENDIGINA ishga tushdi — hali hech qanday frame kelmagan.
    assert p._camera_connected is False, ("P0-4 fix: _camera_connected thread START "
                                          "bo'lgani uchun EMAS, fresh frame kelgani uchun True bo'lishi kerak")
    assert p.status()["camera"]["state"] in ("CONNECTING", "DISCONNECTED", "FAILED", "RECONNECTING")

    p.client.frame_queue.put(b"\x00" * 30)   # birinchi "frame" keldi
    assert wait_until(lambda: p._camera_connected is True, timeout=2.0)
    assert p.status()["camera"]["state"] == "CONNECTED"


# ============================================================
# 9) Camera thread exception state'ni o'zgartiradi
# ============================================================
def test_stream_thread_unexpected_exception_sets_failed_state(raw_pipeline, fake_client_cls, monkeypatch):
    p = raw_pipeline
    # Wrapper xato bo'lgach avtomatik reconnectni rejalashtiradi — bu FAKE
    # klient bilan xavfsiz (real tarmoqqa ULANMAYDI), lekin race'ni kamaytirish
    # uchun keyingi urinishni sekinlashtiramiz (asosiy tekshiruv: thread
    # tugashi bilanoq FAILED/last_error/thread_alive=False ANIQ o'rnatiladi).
    monkeypatch.setattr(cfg.CAMERA, "reconnect_initial_delay_sec", 60.0)

    def _boom():
        raise RuntimeError("kutilmagan ichki xato (test)")

    p._stream_loop = _boom   # _stream_loop ning o'zi kutilmagan xato bilan qulaydi
    with p._camera_lock:
        p._camera_generation += 1
        gen = p._camera_generation
        p._camera_shutting_down = False
    # Wrapperni to'g'ridan-to'g'ri (threadsiz, sinxron) chaqiramiz — determinstik.
    p._stream_loop_wrapper(gen)

    assert p._camera_connected is False
    assert p._camera_thread_alive is False
    assert "kutilmagan ichki xato" in (p.status()["camera"]["last_error"] or "")
    # to'liq holat FAILED (o'tkinchi) -> darhol RECONNECTING ga o'tadi (schedule
    # qilingani uchun) — ikkalasi ham "thread o'limi aniqlandi va reaksiya
    # bor" ekanini isbotlaydi (audit: "avtomatik reconnect YO'Q edi" muammosi).
    assert p.status()["camera"]["state"] in ("FAILED", "RECONNECTING")


# ============================================================
# 10) Stale frame health'ni degraded (STALE) qiladi
# ============================================================
def test_stale_frame_marks_health_stale(raw_pipeline, monkeypatch):
    p = raw_pipeline
    monkeypatch.setattr(cfg.CAMERA, "stale_after_sec", 0.05)
    with p._camera_lock:
        p._camera_connected = True
        p._camera_state = CameraState.CONNECTED
        p._last_frame_received_at = time.time() - 10.0   # "eski" frame

    health = p.status()["camera"]
    assert health["state"] == "STALE", "stale_after_sec dan oshgan frame health'ni STALE qilishi kerak"
    assert health["last_frame_age_ms"] > 1000


def test_fresh_frame_is_not_stale(raw_pipeline, monkeypatch):
    p = raw_pipeline
    monkeypatch.setattr(cfg.CAMERA, "stale_after_sec", 3.0)
    with p._camera_lock:
        p._camera_connected = True
        p._camera_state = CameraState.CONNECTED
        p._last_frame_received_at = time.time()

    health = p.status()["camera"]
    assert health["state"] == "CONNECTED"


# ============================================================
# 16) Decode error threshold'dan keyin reconnect (thread tugaydi -> FAILED)
# ============================================================
def test_decode_error_threshold_triggers_reconnect(raw_pipeline, fake_client_cls, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "decode_bmp", lambda payload: None)   # HAR DOIM decode xatosi
    monkeypatch.setattr(cfg.CAMERA, "max_consecutive_timeouts", 3)
    # Reconnect avtomatik boshlanmasin (bu test faqat threshold/FAILED o'tishini tekshiradi)
    monkeypatch.setattr(cfg.CAMERA, "reconnect_max_attempts", 1)
    monkeypatch.setattr(cfg.CAMERA, "reconnect_initial_delay_sec", 60.0)   # keyingi urinish sekin bo'lsin

    p = raw_pipeline
    p.connect_camera("10.0.0.9")
    for _ in range(5):
        p.client.frame_queue.put(b"\x00" * 10)   # decode_bmp doim None qaytaradi

    assert wait_until(lambda: p.status()["camera"]["consecutive_timeouts"] >= 0
                     and p._camera_thread_alive is False, timeout=3.0), \
        "ketma-ket decode xatosidan keyin stream thread tugashi (FAILED) kerak"
