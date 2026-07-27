"""
Rapid MVP — D2223 EXIT signalining PLC-polling wiring testlari.

PLCService `exit_address` sozlangan bo'lsa har pollda EXIT registrini o'qiydi va
RISING-EDGE da on_exit() (pipeline.on_exit_signal) chaqiradi. Bu testlar:
  * rising-edge da on_exit BIR MARTA chaqirilishini;
  * signal ON bo'lib TURSA qayta chaqirilmasligini (duplicate-edge himoyasi);
  * falling -> rising da QAYTA chaqirilishini;
  * exit_address=None bo'lsa HECH QACHON chaqirilmasligini (single-signal);
  * read xatosi (None) da xavfsiz e'tiborsiz qoldirilishini
tekshiradi. Real qurilma ULANMAYDI (FakeRealPLC), _poll_once() to'g'ridan-to'g'ri.
"""
from __future__ import annotations

import pytest

from backend import config as cfg
from backend.plc.plc_base import PLCInterface
from backend.plc.plc_service import PLCService


class FakeExitPLC(PLCInterface):
    """Asosiy signal doim 0; EXIT registri test tomonidan boshqariladi."""

    def __init__(self):
        self.value = 0                 # asosiy signal (D521) — doim 0 (izolyatsiya)
        self.exit_value = 0            # D2223
        self.exit_read_error = False   # True -> read_register None qaytaradi

    def connect(self) -> bool:
        return True

    def read_signal(self):
        return self.value

    def read_register(self, address: str):
        if self.exit_read_error:
            return None
        return self.exit_value

    def close(self) -> None:
        pass


def _make_service(monkeypatch, exit_address="D2223"):
    monkeypatch.setattr(cfg.PLC, "require_acknowledgement", False)
    monkeypatch.setattr(cfg.PLC, "done_address", None)
    monkeypatch.setattr(cfg.PLC, "write_enabled", False)
    monkeypatch.setattr(cfg.PLC, "trigger_on_value", 1)
    monkeypatch.setattr(cfg.PLC, "startup_recovery_policy", "wait_for_edge")
    monkeypatch.setattr(cfg.PLC, "exit_address", exit_address)
    monkeypatch.setattr(cfg.PLC, "exit_on_value", 1)

    exits = []
    svc = PLCService(on_start=lambda: None, on_stop=lambda: None,
                     on_exit=lambda: exits.append(1))
    fake = FakeExitPLC()
    svc.plc = fake
    svc._connected = True
    svc._poll_once()               # priming: exit=0 -> _last_exit_value=0 (toza edge holati)
    assert exits == []
    return svc, fake, exits


# ------------------------------------------------------------------
# 1) Rising edge -> on_exit BIR MARTA
# ------------------------------------------------------------------
def test_exit_rising_edge_fires_once(monkeypatch):
    svc, fake, exits = _make_service(monkeypatch)
    fake.exit_value = 1
    svc._poll_once()
    assert exits == [1]


# ------------------------------------------------------------------
# 2) Signal ON bo'lib TURSA -> qayta chaqirilmaydi (duplicate-edge himoyasi)
# ------------------------------------------------------------------
def test_exit_held_high_fires_only_once(monkeypatch):
    svc, fake, exits = _make_service(monkeypatch)
    fake.exit_value = 1
    for _ in range(5):
        svc._poll_once()
    assert exits == [1]     # 5 poll davomida ON, lekin faqat BIR marta


# ------------------------------------------------------------------
# 3) Falling -> rising -> QAYTA chaqiriladi (yangi kuzov)
# ------------------------------------------------------------------
def test_exit_falling_then_rising_fires_again(monkeypatch):
    svc, fake, exits = _make_service(monkeypatch)
    fake.exit_value = 1
    svc._poll_once()        # rising #1
    fake.exit_value = 0
    svc._poll_once()        # falling
    fake.exit_value = 1
    svc._poll_once()        # rising #2
    assert exits == [1, 1]


# ------------------------------------------------------------------
# 4) exit_address=None -> on_exit HECH QACHON (single-signal rejim)
# ------------------------------------------------------------------
def test_no_exit_address_never_fires(monkeypatch):
    svc, fake, exits = _make_service(monkeypatch, exit_address=None)
    fake.exit_value = 1
    for _ in range(3):
        svc._poll_once()
    assert exits == []


# ------------------------------------------------------------------
# 5) Read xatosi (None) -> xavfsiz e'tiborsiz, keyin tiklanadi
# ------------------------------------------------------------------
def test_exit_read_error_is_safe(monkeypatch):
    svc, fake, exits = _make_service(monkeypatch)
    fake.exit_read_error = True
    fake.exit_value = 1
    svc._poll_once()        # read None -> e'tiborsiz, crash yo'q
    assert exits == []
    # Tiklanadi: endi o'qish ishlaydi, rising-edge chaqiradi.
    fake.exit_read_error = False
    svc._poll_once()
    assert exits == [1]
