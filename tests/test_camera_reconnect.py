"""
============================================================
tests/test_camera_reconnect.py — P0-4 audit fix: kamera avtomatik reconnect
============================================================
`test_camera_watchdog.py` fundamental holat-mashinasini (thread exception,
stale, connected=true faqat fresh frame) tekshiradi. Bu fayl AYNAN reconnect
loop'ning o'zini: bounded exponential backoff, shutdown to'xtatishi, ikkita
reconnect thread bir vaqtda ochilmasligi, va sessiya ochilganda dead kamera
qayta tiklanishini tekshiradi.

Hech qanday real qurilmaga ULANISH YO'Q — Lector652Client FakeCameraClient
bilan almashtiriladi (monkeypatch).
"""
from __future__ import annotations

import time

import numpy as np
import pytest

import backend.config as cfg
import backend.pipeline as pipeline_mod
from backend.pipeline import CameraState, Pipeline

from conftest import wait_until
from test_camera_watchdog import FakeCameraClient


@pytest.fixture
def fake_client_cls(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "Lector652Client", FakeCameraClient)
    return FakeCameraClient


@pytest.fixture
def fake_decode_ok(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "decode_bmp", lambda payload: np.zeros((8, 8), dtype=np.uint8))


@pytest.fixture
def raw_pipeline(monkeypatch):
    monkeypatch.setattr(cfg.RFID, "enabled", False)
    monkeypatch.setattr(cfg.CAMERA, "stale_after_sec", 0.2)
    monkeypatch.setattr(cfg.CAMERA, "max_consecutive_timeouts", 3)
    monkeypatch.setattr(cfg.CAMERA, "reconnect_initial_delay_sec", 0.03)
    monkeypatch.setattr(cfg.CAMERA, "reconnect_max_delay_sec", 0.15)
    monkeypatch.setattr(cfg.CAMERA, "reconnect_max_attempts", 0)
    p = Pipeline()
    yield p
    try:
        p.disconnect_camera()
    except Exception:
        pass


def _kill_stream(p: "Pipeline") -> None:
    """Joriy stream threadni tezda 'o'ldiradi' — client.read_stream_frame()
    doim xato ko'tarishini ta'minlab, max_consecutive_timeouts chegarasiga
    tezda yetkazadi (thread tabiiy ravishda tugaydi -> wrapper reconnectni
    rejalashtiradi)."""
    for _ in range(10):
        p.client.frame_queue.put(TimeoutError("fake timeout (test)"))


# ============================================================
# 11) Camera reconnect ishlaydi
# ============================================================
def test_camera_reconnects_after_stream_death(raw_pipeline, fake_client_cls, fake_decode_ok):
    p = raw_pipeline
    p.connect_camera("10.0.0.9")
    p.client.frame_queue.put(b"\x00" * 10)   # birinchi frame -> CONNECTED
    assert wait_until(lambda: p._camera_connected is True, timeout=2.0)

    first_client = p.client
    _kill_stream(p)   # stream threadni "o'ldiramiz" (ketma-ket timeout)

    assert wait_until(lambda: p._camera_reconnect_attempts >= 1, timeout=3.0), \
        "kamera o'lgach avtomatik reconnect urinishi boshlanishi kerak"
    # Reconnect muvaffaqiyatli bo'lsa yangi client yaratiladi va yana frame kelib
    # qayta CONNECTED bo'ladi.
    assert wait_until(lambda: p.client is not None and p.client is not first_client, timeout=3.0)
    p.client.frame_queue.put(b"\x00" * 10)
    assert wait_until(lambda: p._camera_connected is True, timeout=2.0)
    assert p.status()["camera"]["state"] == "CONNECTED"


# ============================================================
# 12) Reconnect backoff bounded
# ============================================================
def test_reconnect_backoff_is_bounded(raw_pipeline, fake_client_cls, monkeypatch):
    """Ulanish HAR DOIM muvaffaqiyatsiz bo'lsa (masalan kamera butunlay o'chgan)
    — reconnect urinishlari cheksiz davom etadi, LEKIN kechikish
    reconnect_max_delay_sec dan OSHMAYDI (bounded exponential backoff)."""
    monkeypatch.setattr(cfg.CAMERA, "reconnect_initial_delay_sec", 0.02)
    monkeypatch.setattr(cfg.CAMERA, "reconnect_max_delay_sec", 0.08)
    p = raw_pipeline

    def _always_fail_connect(*a, **kw):
        c = FakeCameraClient(*a, **kw)
        c.connect_should_fail = True
        return c

    monkeypatch.setattr(pipeline_mod, "Lector652Client", _always_fail_connect)
    p.connect_camera("10.0.0.9")   # birinchi urinish muvaffaqiyatsiz -> reconnect boshlanadi

    assert wait_until(lambda: p._camera_reconnect_attempts >= 5, timeout=3.0), \
        "backoff bounded bo'lgani uchun bir necha soniyada bir nechta urinish bo'lishi kerak"
    # Reconnect loop hali ishlamoqda (to'xtab qolmagan) va state FAILED/RECONNECTING.
    assert p.status()["camera"]["state"] in ("RECONNECTING", "FAILED")


# ============================================================
# 13) Shutdown reconnect loopni to'xtatadi
# ============================================================
def test_disconnect_stops_reconnect_loop(raw_pipeline, fake_client_cls, monkeypatch):
    monkeypatch.setattr(cfg.CAMERA, "reconnect_initial_delay_sec", 0.02)

    def _always_fail_connect(*a, **kw):
        c = FakeCameraClient(*a, **kw)
        c.connect_should_fail = True
        return c

    monkeypatch.setattr(pipeline_mod, "Lector652Client", _always_fail_connect)
    p = raw_pipeline
    p.connect_camera("10.0.0.9")
    assert wait_until(lambda: p._camera_reconnect_attempts >= 1, timeout=2.0)

    p.disconnect_camera()
    attempts_after_disconnect = p._camera_reconnect_attempts
    time.sleep(0.2)   # reconnect loop hali "davom etayotgan" bo'lsa shu vaqtda yana urinardi
    assert p._camera_reconnect_attempts == attempts_after_disconnect, \
        "disconnect_camera() dan keyin reconnect loop DARHOL to'xtashi kerak"
    assert p._reconnect_thread is None or not p._reconnect_thread.is_alive()
    assert p.status()["camera"]["state"] == "DISCONNECTED"


# ============================================================
# 14) Ikkita reconnect thread bir vaqtda ochilmaydi
# ============================================================
def test_no_double_reconnect_thread(raw_pipeline, monkeypatch):
    monkeypatch.setattr(cfg.CAMERA, "reconnect_initial_delay_sec", 5.0)   # tezda tugamasin
    p = raw_pipeline
    p._camera_shutting_down = False

    p._schedule_reconnect()
    t1 = p._reconnect_thread
    assert t1 is not None and t1.is_alive()

    p._schedule_reconnect()   # ikkinchi chaqiruv — YANGI thread OCHMASLIGI kerak
    t2 = p._reconnect_thread
    assert t2 is t1, "ikkinchi _schedule_reconnect() chaqiruvi yangi thread OCHMASLIGI kerak"

    p._cancel_reconnect_locked()   # tozalash


# ============================================================
# 17) Sessiya ochilganda dead camera qayta tiklanadi
# 18) Fake camera recover bo'lgach state CONNECTED
# ============================================================
def test_connect_camera_recovers_dead_camera_on_demand(raw_pipeline, fake_client_cls, fake_decode_ok):
    """
    Pipeline._start_session() (sessiya mantiqi — bu agent TEGMAYDIGAN kod)
    har doim `if not self._camera_connected: self.connect_camera(...)`
    chaqiradi. Bu test aynan o'sha chaqiruv yo'lini simulyatsiya qiladi:
    kamera "o'lgan" (FAILED) holatda, keyin YANGI trigger kelganida
    connect_camera() qayta chaqirilsa — kamera qayta tiklanishi va oxir-oqibat
    CONNECTED bo'lishi kerak.
    """
    p = raw_pipeline
    p.connect_camera("10.0.0.9")
    p.client.frame_queue.put(b"\x00" * 10)
    assert wait_until(lambda: p._camera_connected is True, timeout=2.0)

    # Kamera "o'ladi" (masalan tarmoq uzilishi) — barcha keyingi o'qishlar xato.
    for _ in range(10):
        p.client.frame_queue.put(ConnectionError("fake kamera o'ldi (test)"))
    assert wait_until(lambda: p._camera_connected is False, timeout=2.0)

    # YANGI sessiya/trigger kelganda pipeline._start_session xuddi shunday qiladi
    # (bu agent TEGMAGAN sessiya kodi, pipeline.py:~430 atrofida):
    #     if not self._camera_connected: self.connect_camera(CAMERA.ip)
    if not p._camera_connected:
        p.connect_camera("10.0.0.9")

    p.client.frame_queue.put(b"\x00" * 10)
    assert wait_until(lambda: p._camera_connected is True, timeout=3.0), \
        "dead camera keyingi sessiya trigger'ida qayta tiklanishi kerak"
    assert p.status()["camera"]["state"] == "CONNECTED"
