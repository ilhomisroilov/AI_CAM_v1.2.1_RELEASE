"""
============================================================
tests/test_plc_handshake.py — P0-3 audit fix: PLC handshake / trigger-storm
============================================================
Audit topilmasi (plc_service.py:97-103, notify_vin_done): real PLC uchun
`_force_off=True` FAQAT bitta poll siklini "signal=0" deb ALDAR edi — real
registrga (masalan D521) hech narsa yozilmasdi. Agar tashqi PLC signalni
o'zi tozalamasa, keyingi poll'da yana 1 o'qilib YANGI rising-edge sifatida
talqin qilinardi -> bitta kuzov uchun TAKRORIY sessiyalar ("trigger storm").

Bu fayl yangi PLCHandshakeState mashinasini (IDLE/TRIGGER_DETECTED/BUSY/
WAITING_ACK/ACKNOWLEDGED/ERROR) va notify_vin_done() ning yangi (soxta
qiymatsiz) xatti-harakatini tekshiradi.

Hech qanday real qurilmaga ulanish yo'q — FakePLC/PLCSimulator ishlatiladi,
haqiqiy Melsec/tarmoq kodiga tegilmaydi.
"""
from __future__ import annotations

import threading
import time

import pytest

import backend.config as cfg
from backend.plc.plc_base import PLCConfigError, PLCInterface
from backend.plc.plc_service import PLCService, validate_plc_config
from backend.plc.plc_simulator import PLCSimulator


class FakeRealPLC(PLCInterface):
    """
    Haqiqiy (masalan Melsec) PLC ni simulyatsiya qiladi — LEKIN PLCSimulator
    EMAS (shuning uchun notify_vin_done() "soxta set_state(0)" yo'lidan emas,
    "haqiqiy PLC" yo'lidan o'tadi). `value` operator/test tomonidan qo'lda
    boshqariladi — xuddi tashqi ladder logikasi kabi (bizning kodimiz uni
    HECH QACHON to'g'ridan-to'g'ri o'zgartira olmaydi, faqat write_bit orqali
    "so'rov" yuboradi).
    """

    def __init__(self, value: int = 0):
        self.value = value
        self.write_calls: list[tuple[str, int]] = []
        self.connect_calls = 0
        self.connect_results: list[bool] = []  # bo'sh bo'lsa doim True

    def connect(self) -> bool:
        self.connect_calls += 1
        if self.connect_results:
            return self.connect_results.pop(0)
        return True

    def read_signal(self):
        return self.value

    def write_bit(self, address: str, value: int) -> bool:
        self.write_calls.append((address, value))
        return True

    def close(self) -> None:
        pass


@pytest.fixture
def counting_service(monkeypatch):
    """PLCService — build_plc() chaqirilmaydi (FakeRealPLC to'g'ridan-to'g'ri
    biriktiriladi), on_start/on_stop chaqiruvlari sanaladi."""
    monkeypatch.setattr(cfg.PLC, "require_acknowledgement", False)
    monkeypatch.setattr(cfg.PLC, "done_address", None)
    monkeypatch.setattr(cfg.PLC, "write_enabled", False)
    monkeypatch.setattr(cfg.PLC, "trigger_on_value", 1)
    monkeypatch.setattr(cfg.PLC, "startup_recovery_policy", "wait_for_edge")

    starts = []
    stops = []
    svc = PLCService(on_start=lambda: starts.append(1), on_stop=lambda: stops.append(1))
    fake = FakeRealPLC(value=0)
    svc.plc = fake
    svc._connected = True   # ulanish allaqachon bor deb hisoblaymiz (safe_connect chaqirilmasin)
    # Startup/recovery-siyosat testlaridan MUSTAQIL bo'lishi uchun "birinchi poll"
    # ni signal=0 bilan iste'mol qilamiz (xuddi ilova signal OFF holatda ishga
    # tushgandek) — shu bilan keyingi testlar toza edge-detection holatidan
    # boshlanadi. Startup xatti-harakatining o'zi alohida testlarda (4-son)
    # YANGI, priming qilinmagan PLCService orqali tekshiriladi.
    svc._poll_once()
    assert len(starts) == 0 and len(stops) == 0
    return svc, fake, starts, stops


@pytest.fixture
def fresh_service(monkeypatch):
    """
    counting_service dan farqli — HECH QANDAY priming poll qilinmaydi. Faqat
    startup/recovery-siyosat testlari (4-son) uchun, chunki ular ANIQ
    "PLCService ishga tushgandan keyingi BIRINCHI poll" xatti-harakatini
    tekshiradi.
    """
    monkeypatch.setattr(cfg.PLC, "require_acknowledgement", False)
    monkeypatch.setattr(cfg.PLC, "done_address", None)
    monkeypatch.setattr(cfg.PLC, "write_enabled", False)
    monkeypatch.setattr(cfg.PLC, "trigger_on_value", 1)
    monkeypatch.setattr(cfg.PLC, "startup_recovery_policy", "wait_for_edge")

    starts = []
    stops = []
    svc = PLCService(on_start=lambda: starts.append(1), on_stop=lambda: stops.append(1))
    fake = FakeRealPLC(value=0)
    svc.plc = fake
    svc._connected = True
    return svc, fake, starts, stops


# ============================================================
# 1) Signal ON holatda qolsa — bitta session (yangi trigger OCHILMAYDI)
# ============================================================
def test_signal_stays_on_produces_single_session(counting_service):
    svc, fake, starts, stops = counting_service
    fake.value = 1
    for _ in range(5):
        svc._poll_once()
    assert len(starts) == 1, "signal ON holatda qolsa on_start FAQAT BIR MARTA chaqirilishi kerak"
    assert len(stops) == 0


# ============================================================
# 2) ACK olinmaguncha yangi trigger yo'q
# ============================================================
def test_no_new_trigger_until_ack_received(counting_service, monkeypatch):
    """
    require_acknowledgement=True + done_address sozlangan holatda: VIN
    o'qilgach (notify_vin_done) signal HALI ON bo'lib qolsa (haqiqiy PLC
    D521 ni o'zi tozalamagan) — ACK (yoki real off) kelmaguncha YANGI
    trigger OCHILMAYDI.
    """
    monkeypatch.setattr(cfg.PLC, "require_acknowledgement", True)
    monkeypatch.setattr(cfg.PLC, "done_address", "D9000")
    monkeypatch.setattr(cfg.PLC, "write_enabled", False)  # yozuv o'chiq bo'lsa ham ACK holati kutiladi
    monkeypatch.setattr(cfg.PLC, "ack_timeout_ms", 60_000)  # testda timeout tugamasin

    svc, fake, starts, stops = counting_service
    fake.value = 1
    svc._poll_once()
    assert len(starts) == 1

    svc.notify_vin_done()   # VIN tayyor -> WAITING_ACK (real PLC ga soxta 0 YOZILMAYDI)

    # Signal hamon ON (tashqi PLC tozalamagan) — bir necha marta poll qilamiz.
    for _ in range(5):
        svc._poll_once()
    assert len(starts) == 1, "ACK olinmaguncha yangi trigger OCHILMASLIGI kerak"
    assert svc.status()["handshake_state"] == "WAITING_ACK"


# ============================================================
# 3) ACK'dan keyin (yoki signal real 0 ga tushgach) yangi trigger qabul qilinadi
# ============================================================
def test_new_trigger_accepted_after_real_off(counting_service):
    svc, fake, starts, stops = counting_service
    fake.value = 1
    svc._poll_once()
    assert len(starts) == 1

    svc.notify_vin_done()
    fake.value = 1
    svc._poll_once()          # hali ON -> yangi trigger yo'q
    assert len(starts) == 1

    fake.value = 0            # tashqi PLC nihoyat tozaladi (haqiqiy off)
    svc._poll_once()
    assert len(stops) == 1
    assert svc.status()["handshake_state"] == "IDLE"

    fake.value = 1            # yangi kuzov -> yangi rising edge
    svc._poll_once()
    assert len(starts) == 2, "real off dan keyingi yangi rising-edge qabul qilinishi kerak"


# ============================================================
# 4) Startup: signal ALLAQACHON ON bo'lsa — recovery policy
# ============================================================
def test_startup_signal_already_on_wait_for_edge_default(fresh_service):
    """Standart siyosat (wait_for_edge): startup'da signal ON bo'lsa DARHOL
    START chaqirilmaydi — stale/mid-flight signalni yangi trigger deb
    aralashtirmaslik uchun."""
    svc, fake, starts, stops = fresh_service
    fake.value = 1                 # ilova ishga tushganda signal ALLAQACHON ON
    svc._poll_once()
    assert len(starts) == 0, "wait_for_edge: startup'da ON topilgan signal darhol START chaqirmasligi kerak"
    assert svc.status()["handshake_state"] == "BUSY"

    fake.value = 0                 # signal haqiqiy OFF ga tushdi
    svc._poll_once()
    assert svc.status()["handshake_state"] == "IDLE"

    fake.value = 1                 # ENDI haqiqiy yangi rising-edge
    svc._poll_once()
    assert len(starts) == 1, "signal 0 dan keyin yangi rising-edge qabul qilinishi kerak"


def test_startup_signal_on_start_immediately_policy(fresh_service, monkeypatch):
    """Ongli ravishda 'start_immediately' tanlansa — eski xatti-harakat: startup'da
    signal ON bo'lsa darhol START chaqiriladi."""
    monkeypatch.setattr(cfg.PLC, "startup_recovery_policy", "start_immediately")
    svc, fake, starts, stops = fresh_service
    fake.value = 1
    svc._poll_once()
    assert len(starts) == 1


# ============================================================
# 6) PLC disconnect/reconnect
# ============================================================
def test_plc_disconnect_reconnect_backoff(monkeypatch):
    monkeypatch.setattr(cfg.PLC, "reconnect_max_delay_sec", 30.0)
    starts = []
    stops = []
    svc = PLCService(on_start=lambda: starts.append(1), on_stop=lambda: stops.append(1))
    fake = FakeRealPLC(value=0)
    fake.connect_results = [False, False, True]   # ikki marta muvaffaqiyatsiz, keyin ulanadi
    svc.plc = fake
    svc._connected = False

    # 1) connect muvaffaqiyatsiz -> backoff kutiladi, davom etish kerakligini bildiradi
    monkeypatch.setattr(svc._stop_event, "wait", lambda t: False)  # real vaqt kutmasin
    waited = svc._poll_once()
    assert waited is True
    assert svc._connected is False

    waited = svc._poll_once()
    assert waited is True
    assert svc._connected is False

    # 2) uchinchi urinish -> ulanadi
    waited = svc._poll_once()
    assert svc._connected is True
    assert fake.connect_calls == 3


# ============================================================
# 7) write disabled bo'lsa real PLC write bajarilmaydi
# ============================================================
def test_write_disabled_no_real_plc_write(counting_service, monkeypatch):
    monkeypatch.setattr(cfg.PLC, "write_enabled", False)
    monkeypatch.setattr(cfg.PLC, "done_address", "D9000")
    svc, fake, starts, stops = counting_service

    svc.notify_vin_done()
    assert fake.write_calls == [], "write_enabled=false bo'lsa real PLC ga HECH QANDAY yozuv bajarilmasligi kerak"


def test_write_enabled_writes_done_address(counting_service, monkeypatch):
    monkeypatch.setattr(cfg.PLC, "write_enabled", True)
    monkeypatch.setattr(cfg.PLC, "done_address", "D9000")
    svc, fake, starts, stops = counting_service

    svc.notify_vin_done()
    assert fake.write_calls == [("D9000", 1)], "write_enabled=true bo'lsa done_address ga ACK yozilishi kerak"


# ============================================================
# 8) done_address yo'q + require_acknowledgement=true -> config validation FAIL
# ============================================================
def test_config_validation_fails_without_done_address(monkeypatch):
    monkeypatch.setattr(cfg.PLC, "require_acknowledgement", True)
    monkeypatch.setattr(cfg.PLC, "done_address", None)
    with pytest.raises(PLCConfigError):
        validate_plc_config()
    with pytest.raises(PLCConfigError):
        PLCService(on_start=lambda: None, on_stop=lambda: None)


def test_config_validation_passes_with_done_address(monkeypatch):
    monkeypatch.setattr(cfg.PLC, "require_acknowledgement", True)
    monkeypatch.setattr(cfg.PLC, "done_address", "D9000")
    validate_plc_config()   # raise qilmasligi kerak
    svc = PLCService(on_start=lambda: None, on_stop=lambda: None)
    assert svc is not None


def test_config_validation_passes_by_default(monkeypatch):
    """Standart konfiguratsiya (require_acknowledgement=False) — done_address
    NOMA'LUM bo'lsa ham server ISHGA TUSHISHI kerak (xavfsiz fallback)."""
    monkeypatch.setattr(cfg.PLC, "require_acknowledgement", False)
    monkeypatch.setattr(cfg.PLC, "done_address", None)
    validate_plc_config()


# ============================================================
# Bonus: ACK timeout ishlaydi (require_acknowledgement=True, signal ON qolib
# ketsa va ack_timeout_ms tugasa) — ERROR holatiga o'tadi, lekin YANGI TRIGGER
# hamon OCHILMAYDI (faqat real off orqali tiklanadi).
# ============================================================
def test_ack_timeout_transitions_to_error_without_opening_new_trigger(counting_service, monkeypatch):
    monkeypatch.setattr(cfg.PLC, "require_acknowledgement", True)
    monkeypatch.setattr(cfg.PLC, "done_address", "D9000")
    monkeypatch.setattr(cfg.PLC, "write_enabled", False)
    monkeypatch.setattr(cfg.PLC, "ack_timeout_ms", 3000)

    svc, fake, starts, stops = counting_service
    fake.value = 1
    svc._poll_once()
    assert len(starts) == 1

    svc.notify_vin_done()
    # Real vaqt kutish (sleep) O'RNIGA — deadline'ni to'g'ridan-to'g'ri o'tmishga
    # suramiz (deterministik, flaky BO'LMAYDI: real wall-clock timing'ga
    # bog'liq emas).
    import time as _time
    with svc._hlock:
        svc._ack_deadline = _time.monotonic() - 1.0

    fake.value = 1   # signal hamon ON
    svc._poll_once()
    assert svc.status()["handshake_state"] == "ERROR"
    assert svc.status()["ack_timeout_count"] == 1

    # ERROR holatida ham signal ON qolsa yangi trigger OCHILMAYDI.
    for _ in range(3):
        svc._poll_once()
    assert len(starts) == 1

    # Faqat real off orqali tiklanadi.
    fake.value = 0
    svc._poll_once()
    assert svc.status()["handshake_state"] == "IDLE"
    fake.value = 1
    svc._poll_once()
    assert len(starts) == 2
