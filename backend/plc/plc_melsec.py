"""
============================================================
plc_melsec.py  —  Mitsubishi Q-PLC (MC protokol) klienti
============================================================
MelsecMcGateway metodologiyasi asosida:
  * pymcprotocol Type3E (binary MC protokol), TCP port 5004.
  * Bit registerni o'qish: batchread_bitunits(headdevice, readsize=1) -> [0|1].
  * Ulanish uzilsa qayta ulanadi (PLCService backoff bilan boshqaradi).

pymcprotocol FAQAT melsec rejimida (lazy import) kerak — simulyator rejimida
bu kutubxona o'rnatilmagan bo'lsa ham ilova ishlaydi.
"""
from __future__ import annotations

import re
import threading
from typing import Optional

from ..logger import log
from .plc_base import PLCInterface

# Mitsubishi SO'Z (word) qurilmalari — bularni BIT komandasi bilan o'qib BO'LMAYDI
# (MC protokol 0xC05C/0xC060 xatosi). Qolganlari (X,Y,M,L,B,F,SM,SB...) BIT qurilma.
_WORD_DEVICES = ("ZR", "SD", "SW", "SN", "TN", "CN", "D", "W", "R", "Z")


def _is_word_device(address: str) -> bool:
    """'D521' -> True (word), 'B0901'/'M100' -> False (bit). Prefiks harflari bo'yicha."""
    m = re.match(r"^\s*([A-Za-z]+)", address or "")
    if not m:
        return False
    pfx = m.group(1).upper()
    return pfx in _WORD_DEVICES


class MelsecPLC(PLCInterface):
    name = "melsec"

    def __init__(self, ip: str, port: int, signal_address: str,
                 plc_type: str = "Q") -> None:
        self.ip = ip
        self.port = port
        self.signal_address = signal_address
        self.plc_type = plc_type
        self._is_word = _is_word_device(signal_address)
        self._mc = None
        self._lock = threading.Lock()
        self._connected = False

    def connect(self) -> bool:
        try:
            import pymcprotocol           # lazy — faqat melsec rejimida kerak
        except Exception as exc:
            log.error(f"pymcprotocol import qilinmadi: {exc}. "
                      f"O'rnating: pip install pymcprotocol")
            return False
        try:
            with self._lock:
                # Eski soketni yopamiz — aks holda u "sizib" qoladi va PLC yangi
                # ulanishni rad etadi (timeout). Bu reconnect bo'ronini oldini oladi.
                if self._mc is not None:
                    try:
                        self._mc.close()
                    except Exception:
                        pass
                    self._mc = None
                mc = pymcprotocol.Type3E(plctype=self.plc_type)
                mc.connect(self.ip, self.port)
                self._mc = mc
                self._connected = True
            dtype = "word" if self._is_word else "bit"
            log.info(f"PLC ulandi (MC): {self.ip}:{self.port} [{self.plc_type}] "
                     f"signal={self.signal_address} ({dtype})")
            return True
        except Exception as exc:
            log.error(f"PLC ulanishi muvaffaqiyatsiz ({self.ip}:{self.port}): {exc}")
            self._connected = False
            return False

    @property
    def connected(self) -> bool:
        return self._connected and self._mc is not None

    def read_signal(self) -> Optional[int]:
        """Asosiy signal registerni o'qiydi (self.signal_address)."""
        return self.read_signal_at(self.signal_address)

    def read_signal_at(self, address: str) -> Optional[int]:
        """
        Berilgan MANZILdagi registerni o'qiydi (dual-trigger uchun ham).
          * BIT qurilma (B/M/X/Y...) -> batchread_bitunits -> 0/1.
          * SO'Z qurilma (D/W/R...)  -> batchread_wordunits -> xom qiymat.
        Xato bo'lsa None (qayta ulanish kerak).
        """
        if self._mc is None:
            return None
        addr = address or self.signal_address
        try:
            with self._lock:
                if _is_word_device(addr):
                    values = self._mc.batchread_wordunits(headdevice=addr, readsize=1)
                    return int(values[0]) if values else 0
                values = self._mc.batchread_bitunits(headdevice=addr, readsize=1)
                return 1 if (values and int(values[0])) else 0
        except Exception as exc:
            log.warning(f"PLC o'qish xatosi ({addr}): {exc}")
            self._connected = False
            return None

    def write_signal(self, address: str, value: int) -> bool:
        """
        DONE/handshake registerini yozadi (P6 fix). WORD qurilma (D/W/R...) bo'lsa
        batchwrite_wordunits; BIT qurilma (M/Y/B...) bo'lsa batchwrite_bitunits.
        Xato bo'lsa False (ulanish keyingi siklда tiklanadi).
        """
        if self._mc is None or not address:
            return False
        try:
            with self._lock:
                if _is_word_device(address):
                    self._mc.batchwrite_wordunits(headdevice=address, values=[int(value)])
                else:
                    self._mc.batchwrite_bitunits(headdevice=address,
                                                 values=[1 if int(value) else 0])
            return True
        except Exception as exc:
            log.warning(f"PLC yozish xatosi ({address}={value}): {exc}")
            return False

    def close(self) -> None:
        with self._lock:
            if self._mc is not None:
                try:
                    self._mc.close()
                except Exception:
                    pass
                self._mc = None
            self._connected = False
