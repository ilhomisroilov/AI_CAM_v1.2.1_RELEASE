"""
Rapid MVP — Tier 5: PLC chaos (qolgan holatlar).

Ko'p Tier-5 holati avvalgi remediation/chaos testlarida qoplangan:
  * disconnect/reconnect -> test_plc_disconnect.py
  * signal ON stuck (trigger storm) -> test_plc_handshake.py
  * rapid burst, D2223-before-D2222, no-D2223, duplicate D2223 -> test_chaos_dual_signal.py / test_session_grace.py

Bu fayl QOLGAN ikkitasini qoplaydi:
  * "boshqa WORD bitlari ON" — qat'iy `value == 1` solishtirish O'RNIGA bit/mask
    (spec §10: whole-WORD qat'iy solishtirish ishlatilmasin);
  * read exception (mid-stream None) -> reconnect, trigger yo'qolmaydi.
"""
from __future__ import annotations

import pytest

from backend import config as cfg
from backend.plc.plc_base import PLCInterface
from backend.plc.plc_service import PLCService, PLCHandshakeState


class _WordPLC(PLCInterface):
    """WORD registr qiymatini qaytaradi (bir nechta bit ON bo'lishi mumkin)."""
    def __init__(self, value=0):
        self.value = value
        self.read_error = False

    def connect(self):
        return True

    def read_signal(self):
        if self.read_error:
            return None
        return self.value

    def close(self):
        pass


def _svc(monkeypatch, **plc):
    monkeypatch.setattr(cfg.PLC, "require_acknowledgement", False)
    monkeypatch.setattr(cfg.PLC, "done_address", None)
    monkeypatch.setattr(cfg.PLC, "write_enabled", False)
    monkeypatch.setattr(cfg.PLC, "startup_recovery_policy", "wait_for_edge")
    monkeypatch.setattr(cfg.PLC, "exit_address", None)
    for k, v in plc.items():
        monkeypatch.setattr(cfg.PLC, k, v)
    starts, stops = [], []
    svc = PLCService(on_start=lambda: starts.append(1), on_stop=lambda: stops.append(1))
    fake = _WordPLC(value=0)
    svc.plc = fake
    svc._connected = True
    svc._poll_once()                # priming (value=0)
    assert starts == [] and stops == []
    return svc, fake, starts, stops


# ------------------------------------------------------------------
# "Boshqa WORD bitlari ON" — bit_or_mask rejimida target bit tekshiriladi
# ------------------------------------------------------------------
def test_other_word_bits_on_still_triggers_bit_mode(monkeypatch):
    # trigger_kind=bit_or_mask, trigger_bit=0. value=5 (binary 101) -> bit0 ON.
    svc, fake, starts, stops = _svc(
        monkeypatch, trigger_kind="bit_or_mask", trigger_bit=0, trigger_on_value=1)
    fake.value = 5                  # bit0 ON + boshqa bitlar (bit2) ham ON
    svc._poll_once()
    assert starts == [1], "target bit ON bo'lsa boshqa bitlar ham ON bo'lsa ham trigger bo'lishi kerak"
    # Target bit OFF (value=4 = binary 100, bit0=0) -> STOP.
    fake.value = 4
    svc._poll_once()
    assert stops == [1]


def test_bit_mode_target_bit_off_is_not_trigger(monkeypatch):
    # value=6 (110): bit0=0 -> trigger EMAS (boshqa bitlar ON bo'lsa ham).
    svc, fake, starts, stops = _svc(
        monkeypatch, trigger_kind="bit_or_mask", trigger_bit=0, trigger_on_value=1)
    fake.value = 6
    svc._poll_once()
    assert starts == []


def test_level_mode_backward_compat(monkeypatch):
    # trigger_kind=level (standart): value == trigger_on_value qat'iy (eski xatti-harakat).
    svc, fake, starts, stops = _svc(
        monkeypatch, trigger_kind="level", trigger_on_value=1)
    fake.value = 1
    svc._poll_once()
    assert starts == [1]
    fake.value = 0
    svc._poll_once()
    assert stops == [1]


# ------------------------------------------------------------------
# Read exception (mid-stream None) -> reconnect, trigger yo'qolmaydi
# ------------------------------------------------------------------
def test_read_exception_reconnects_and_trigger_not_lost(monkeypatch):
    svc, fake, starts, stops = _svc(monkeypatch, trigger_kind="level", trigger_on_value=1)
    # O'qish xatosi (None) -> _poll_once True qaytaradi (reconnect), _connected=False.
    fake.read_error = True
    waited = svc._poll_once()
    assert waited is True
    assert svc._connected is False
    # Tiklanadi: keyingi poll connect qiladi, keyingisi trigger ni ko'radi (yo'qolmadi).
    fake.read_error = False
    svc._poll_once()                # reconnect (value hali 0)
    fake.value = 1
    svc._poll_once()                # rising -> START
    assert starts == [1]
