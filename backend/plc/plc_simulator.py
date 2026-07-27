"""
============================================================
plc_simulator.py  —  Soxta PLC (test paneli uchun)
============================================================
Haqiqiy PLC bo'lmasdan butun ish jarayonini sinash imkonini beradi.
Holat (0/1) UI tugmalari orqali (server API) o'rnatiladi. PLCService
buni xuddi haqiqiy PLC bit signali kabi o'qiydi.

mode="melsec" ga o'tilganda kod o'zgarmaydi — faqat config almashtiriladi.
"""
from __future__ import annotations

import threading
from typing import Optional

from .plc_base import PLCInterface


class PLCSimulator(PLCInterface):
    name = "simulator"

    def __init__(self, initial: int = 0) -> None:
        self._state = 1 if int(initial) else 0
        self._lock = threading.Lock()
        # P5 FIX: qisqa pulslarni LATCH qilish. Haqiqiy liniyada ARRIVE biti PLC
        # tomonda latch qilinadi (mashina o'tguncha 1 da turadi). Simulyator shu
        # xulqni modellashtiradi: poll davridan qisqa puls (1->0) o'tkazib
        # yuborilmasin — ko'tarilish qirrasi keyingi o'qishda bir marta ko'rsatiladi.
        self._rising_latched = False
        # Dual-trigger: manzil bo'yicha holat + latch ({address: [state, latched]}).
        self._addr = {}

    def connect(self) -> bool:
        return True

    @property
    def connected(self) -> bool:
        return True

    def set_state(self, value: int) -> None:
        """UI 'PLC ON/OFF' tugmalari uchun joriy levelni o'rnatadi.

        Qisqa, PLC-tomon latch qilingan impulsni modellashtirish uchun ``pulse()``
        ishlatiladi. Oddiy ``set_state(1); set_state(0)`` esa poll oralig'ida
        yo'qolishi mumkin bo'lgan latchsiz fizik leveldir.
        """
        with self._lock:
            v = 1 if int(value) else 0
            self._state = v

    def set_state_at(self, address: str, value: int) -> None:
        """Dual-trigger: berilgan MANZIL holatini o'rnatadi (latch bilan)."""
        with self._lock:
            v = 1 if int(value) else 0
            st = self._addr.get(address, [0, False])
            if v == 1 and st[0] == 0:
                st[1] = True                  # rising latch
            st[0] = v
            self._addr[address] = st

    def pulse(self) -> None:
        """Latch one short rising pulse for the next poll without sleeping."""
        with self._lock:
            self._state = 0
            self._rising_latched = True

    def read_signal(self) -> Optional[int]:
        with self._lock:
            if self._state == 1:
                return 1
            if self._rising_latched:          # o'tkazib yuborilgan qisqa pulsni qaytaramiz
                self._rising_latched = False
                return 1
            return 0

    def read_signal_at(self, address: str) -> Optional[int]:
        with self._lock:
            st = self._addr.get(address)
            if st is None:
                return 0
            if st[0] == 1:
                return 1
            if st[1]:
                st[1] = False
                return 1
            return 0

    def close(self) -> None:
        pass
