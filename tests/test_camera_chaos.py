"""
Rapid MVP — Tier 6: Camera chaos (qolgan holatlar).

Kamera ishonchliligi asosan avvalgi remediation'da qoplangan
(test_camera_watchdog.py / test_camera_reconnect.py): stream exception ->
FAILED, stale-frame health, decode-error threshold -> reconnect, reconnect
after death, bounded backoff, disconnect stops loop, no double thread, dead
camera revived on demand.

Bu fayl QOLGAN Tier-6 assertion'larni qoplaydi:
  * kamera muammosi SESSION MANAGER ni yoki FastAPI status() ni TO'XTATMAYDI
    (kamerasiz sessiya baribir deterministik yopiladi -> TIMEOUT record);
  * MJPEG 1/5/10 klient bir vaqtda live-frame o'qishi bloklamaydi/xato bermaydi.

Real qurilma ULANMAYDI; tmp_path DB; deterministik.
"""
from __future__ import annotations

import threading

import pytest

from backend import config as cfg
from backend.database import db
from backend.pipeline import SessionState
from conftest import wait_until

_TERMINAL = (SessionState.SUCCESS, SessionState.PARTIAL_SUCCESS,
             SessionState.TIMEOUT, SessionState.FAILED, SessionState.CANCELLED)


def _active(p):
    with p._session_lock:
        return p._sessions.get(p._active_session_id) if p._active_session_id else None


def test_camera_failure_does_not_block_session_manager(pipeline_instance, monkeypatch):
    """
    Kamera VIN bermasa (nosoz/o'lik) — sessiya manager BLOKLANMAYDI: sessiya
    hard-deadline bilan yopiladi (TIMEOUT record) va status() javob beradi.
    """
    monkeypatch.setattr(cfg.SESSION, "exit_signal_enabled", True)
    monkeypatch.setattr(cfg.SESSION, "max_session_duration_sec", 0.5)
    p = pipeline_instance
    p.plc_on()
    sess = _active(p)
    # Kamera VIN bermaydi (OCR result inject qilinmaydi) — session manager kutmaydi,
    # hard-deadline failsafe deterministik yopadi.
    assert wait_until(lambda: sess.state in _TERMINAL, timeout=8.0)
    assert sess.state == SessionState.TIMEOUT
    rows = db.get_all_records()
    assert len(rows) == 1 and rows[0]["status"] == "TIMEOUT"
    # FastAPI status() kamera nosozligida ham crash qilmasdan javob beradi.
    st = p.status()
    assert "camera" in st and "session" in st


def test_status_works_when_camera_state_unknown(pipeline_instance):
    """status() kamera hech qachon ulanmagan holatda ham xato bermaydi (health defensive)."""
    p = pipeline_instance
    p._camera_connected = False
    st = p.status()
    assert st["camera"]["state"] is not None      # CameraState.DISCONNECTED va h.k.
    assert "last_frame_age_ms" in st["camera"]


@pytest.mark.parametrize("n_clients", [1, 5, 10])
def test_mjpeg_multiclient_concurrent_read(pipeline_instance, n_clients):
    """
    MJPEG 1/5/10 klient: get_jpeg() ni bir vaqtda ko'p thread o'qiydi — bloklamaydi,
    xato bermaydi, izchil bytes qaytaradi (frame_lock ostida).
    """
    p = pipeline_instance
    with p._frame_lock:
        p._live_jpeg = b"\xff\xd8\xff\xe0FAKEJPEG\xff\xd9"

    errors = []
    reads = []
    barrier = threading.Barrier(n_clients)

    def _client():
        try:
            barrier.wait(timeout=5)
            for _ in range(50):
                data = p.get_jpeg()
                reads.append(data)
        except Exception as exc:        # noqa: BLE001 - test diagnostikasi
            errors.append(repr(exc))

    threads = [threading.Thread(target=_client) for _ in range(n_clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert len(reads) == n_clients * 50
    # Barcha o'qishlar bir xil (izchil) frame — buzilmagan.
    assert all(d == b"\xff\xd8\xff\xe0FAKEJPEG\xff\xd9" for d in reads)
