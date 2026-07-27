"""
============================================================
plc_base.py  —  PLC manba interfeysi (abstraksiya)
============================================================
Bitta interfeys ortida ikkita amalga oshirish:
  * PLCSimulator  — UI tugmalari bilan boshqariladigan soxta PLC (test).
  * MelsecPLC     — haqiqiy Mitsubishi Q-PLC (MC protokol, gateway_python uslubida).

PLCService shu interfeysga tayanadi, shuning uchun simulyatordan haqiqiy
PLC ga o'tish FAQAT config (plc.mode) o'zgartirish bilan amalga oshadi.
"""
from __future__ import annotations

from typing import Optional


class PLCConfigError(ValueError):
    """Raised when an enabled PLC handshake cannot be configured safely."""


class PLCInterface:
    """Barcha PLC manbalar uchun umumiy shartnoma."""

    name: str = "plc"

    def connect(self) -> bool:
        """Ulanadi. Muvaffaqiyatli bo'lsa True."""
        return True

    def read_signal(self) -> Optional[int]:
        """
        Kuzatiladigan bit signalini o'qiydi: 0 yoki 1.
        Xato bo'lsa None (PLCService qayta ulanadi).
        """
        raise NotImplementedError

    def read_signal_at(self, address: str) -> Optional[int]:
        """
        Berilgan MANZILdagi signalni o'qiydi (dual-trigger uchun). Standart —
        asosiy read_signal() ga tushadi (bitta manzilli manbalar uchun).
        """
        return self.read_signal()

    def read_register(self, address: str) -> Optional[int]:
        """Compatibility alias for hardening/HIL code using register wording."""
        return self.read_signal_at(address)

    def write_signal(self, address: str, value: int) -> bool:
        """
        Handshake/DONE bitini yoki registerni yozadi (P6 fix). Qo'llab-quvvatlanmasa
        False. Simulyatorда no-op (True).
        """
        return False

    def write_bit(self, address: str, value: int) -> bool:
        """Compatibility alias; concrete transports keep one write path."""
        return self.write_signal(address, value)

    def close(self) -> None:
        """Resurslarni bo'shatadi."""
        pass

    @property
    def connected(self) -> bool:
        return True
