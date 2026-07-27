"""
============================================================
rfid_service.py  —  RFID orkestrator (UI ni HECH QACHON bloklamaydi)
============================================================
gateway_python ArriveProcessor uslubida:
  * Fon SSE oqim tinglovchi (R700) doimo teglarni keshga yig'adi + avto-reconnect.
  * PLC=1 (trigger) kelganda `trigger_read()` ALOHIDA worker threadda o'qiydi —
    kamera ishlov berishni ham, PLC pollingni ham bloklamaydi (parallel).
  * O'qish tugagach `on_done(epc_raw, rfid_number, ts)` callback chaqiriladi.
  * Status: ulanish, holat (IDLE/READING/OK/NO_TAG/ERROR), oxirgi teg + vaqt.

Xatoliklar yumshoq (graceful) — o'qish muvaffaqiyatsiz bo'lsa bo'sh natija
qaytadi, ilova qulamaydi. Barcha hodisalar log qilinadi.
"""
from __future__ import annotations

import inspect
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from ..config import RFID
from ..logger import log
from .epc_extract import extract_rfid_number
from .rfid_reader import build_reader

# RFID integrity sentinellari — qiymat O'YLAB TOPILMAYDI; teg bo'lmasa/xato bo'lsa
# shu aniq belgilar saqlanadi (random/taxminiy raqam EMAS).
NO_TAG = "NO_TAG"      # reader ishladi, lekin teg topilmadi
NO_READ = "NO_READ"    # reader urindi, lekin o'qish muvaffaqiyatsiz (xato/ulanish)


@dataclass(frozen=True)
class RFIDResult:
    """Session-owned RFID result passed across the worker boundary."""

    session_id: Optional[str]
    epc: str
    number: str
    antenna: Optional[int]
    rssi: Optional[float]
    first_seen_at: datetime
    received_at: datetime


class RFIDService:
    def __init__(self) -> None:
        self.reader = build_reader()
        self._busy = threading.Lock()        # bir vaqtda bitta o'qish
        self._stop_event = threading.Event()
        self._started = False
        self._state = "IDLE"                  # IDLE | READING | OK | NO_TAG | ERROR
        self._last_epc: str = ""              # oxirgi xom EPC
        self._last_number: str = ""           # oxirgi RFID raqami (1000..9999)
        self._last_ts: Optional[datetime] = None

    # ---------------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        try:
            self.reader.start()               # R700: fon oqim tinglovchi; sim: no-op
        except Exception as exc:
            log.error(f"RFID start xatosi: {exc}")
        log.info(f"RFID xizmati ishga tushdi (mode={RFID.mode}, "
                 f"ip={RFID.ip_address}:{RFID.port}, read={RFID.read_duration_ms}ms).")

    def stop(self) -> None:
        self._started = False
        self._stop_event.set()
        try:
            self.reader.stop()
        except Exception:
            pass
        log.info("RFID xizmati to'xtatildi.")

    # ---------------------------------------------------------------
    # PLC=1 da chaqiriladi — ALOHIDA threadda o'qiydi (parallel, non-blocking)
    # ---------------------------------------------------------------
    def trigger_read(self, on_done: Optional[Callable[..., None]] = None,
                     stop_event: Optional[threading.Event] = None,
                     deadline: Optional[float] = None,
                     session_id: Optional[str] = None,
                     started_at: Optional[float] = None) -> bool:
        """
        PLC=1 da chaqiriladi. ALOHIDA threadda gateway uslubidagi QAYTA URINISH
        siklini ishga tushiradi: teg o'qilmasa, `deadline` (monotonic) ga
        yetguncha qayta-qayta urinadi (kamerani ham, PLC pollingni ham bloklamaydi).
          stop_event — sessiya to'xtatilsa darhol tugaydi (None -> ichki stop).
          deadline   — monotonic vaqt; berilmagan bo'lsa gateway-budget ishlatiladi
                       (6s + 2×(2s+4s) ≈ 18s).
          session_id — natija egaligi; RFIDResult bilan aynan qaytariladi.
          started_at — audit caller compatibility maydoni (worker clockini almashtirmaydi).
        """
        if not RFID.enabled:
            return False
        if not self._busy.acquire(blocking=False):
            log.warning("RFID: oldingi o'qish hali tugamagan — trigger o'tkazib yuborildi.")
            return False
        self._state = "READING"
        ev = stop_event if stop_event is not None else self._stop_event
        if deadline is None:
            budget = (RFID.inventory_duration_ms
                      + 2 * (RFID.inventory_retry_after_ms + RFID.inventory_retry_duration_ms))
            deadline = time.monotonic() + budget / 1000.0
        t = threading.Thread(target=self._do_read,
                             args=(on_done, ev, deadline, session_id),
                             name="rfid-read", daemon=True)
        t.start()
        return True

    def _read_until_deadline(self, stop_event, deadline) -> Optional[str]:
        """
        gateway ArriveProcessor._run_inventory_until_tag AYNAN mantig'i, lekin
        qat'iy retry_count o'rniga sessiya `deadline` gacha davom etadi:
          * 1-urinish: inventory_duration_ms (6000) oynasi,
          * keyin: inventory_retry_after_ms (2000) pauza + inventory_retry_duration_ms
            (4000) oynasi — deadline ga yetguncha qaytariladi.
        """
        stats = {"ok": 0, "err": 0, "last": None}

        def attempt(idx: int, dur_ms: int):
            """Bitta urinish — ulanish/o'qish xatosini YUMSHOQ qiladi (qayta urinish uchun)."""
            log.info(f"[RFID] o'qish urinishi #{idx} (oyna={dur_ms}ms).")
            try:
                e = self.reader.read_tag(stop_event, duration_ms=dur_ms, deadline=deadline)
                stats["ok"] += 1
                return e
            except Exception as exc:
                stats["err"] += 1
                stats["last"] = exc
                log.warning(f"[RFID] urinish #{idx} ulanish/o'qish xatosi (qayta urinadi): {exc}")
                return None

        idx = 1
        epc = attempt(idx, RFID.inventory_duration_ms)
        if epc:
            log.info(f"[RFID] teg topildi (urinish #{idx}): EPC={epc}")
            return epc

        gap_s = max(0.0, RFID.inventory_retry_after_ms / 1000.0)
        while not stop_event.is_set() and time.monotonic() < deadline:
            # Pauza (deadline bilan cheklangan)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if stop_event.wait(min(gap_s, remaining)):
                return None
            if time.monotonic() >= deadline:
                break
            idx += 1
            epc = attempt(idx, RFID.inventory_retry_duration_ms)
            if epc:
                log.info(f"[RFID] teg topildi (urinish #{idx}): EPC={epc}")
                return epc
            log.info(f"[RFID] urinish #{idx}: teg topilmadi — qayta urinadi.")

        # Teg topilmadi. Agar BIRORTA toza oyna ham bo'lmagan bo'lsa (reader umuman
        # ulanmadi) -> xato ko'taramiz (-> NO_READ). Aks holda teg yo'q (-> NO_TAG).
        if stats["ok"] == 0 and stats["last"] is not None:
            log.error(f"[RFID] {idx} urinish — reader bilan ulanish yo'q -> NO_READ.")
            raise stats["last"]
        log.info(f"[RFID] {idx} urinishdan keyin teg topilmadi (deadline/stop) -> NO_TAG.")
        return None

    def _do_read(self, on_done, stop_event, deadline,
                 session_id: Optional[str] = None) -> None:
        first_seen = datetime.now()
        ts = first_seen
        epc = ""               # xom EPC ("" = saqlanmaydi -> NULL)
        number = ""
        try:
            raw = self._read_until_deadline(stop_event, deadline)
            ts = datetime.now()
            if raw and str(raw).strip():
                # HAQIQIY EPC o'qildi -> faqat shu holatda raqam ajratiladi
                epc = str(raw).strip()
                number = extract_rfid_number(epc, int(RFID.epc_decimal_min), int(RFID.epc_decimal_max))
                if number:
                    self._state = "OK"
                    log.info(f"RFID o'qildi: EPC={epc} -> RFID_EPC={number}")
                else:
                    # Validdek EPC, lekin raqam chiqmadi — qiymat O'YLAB TOPILMAYDI
                    epc = ""
                    number = NO_TAG
                    self._state = NO_TAG
                    log.info("RFID: EPC dan raqam ajratilmadi -> NO_TAG.")
            else:
                # Reader ishladi, lekin oynada teg yo'q
                epc = ""
                number = NO_TAG
                self._state = NO_TAG
                log.info("RFID: teg topilmadi -> NO_TAG.")
        except Exception as exc:
            # Reader urindi, lekin XATO (ulanish/o'qish) -> NO_READ (taxmin EMAS)
            epc = ""
            number = NO_READ
            self._state = NO_READ
            log.error(f"RFID o'qish muvaffaqiyatsiz -> NO_READ: {exc}")
        finally:
            self._last_epc = epc
            self._last_number = number
            self._last_ts = ts
            self._busy.release()
            if on_done is not None:
                result = RFIDResult(
                    session_id=session_id,
                    epc=epc,
                    number=number,
                    antenna=None,
                    rssi=None,
                    first_seen_at=first_seen,
                    received_at=ts,
                )
                try:
                    legacy_callback = False
                    try:
                        inspect.signature(on_done).bind(result)
                    except TypeError:
                        inspect.signature(on_done).bind(epc, number, ts)
                        legacy_callback = True
                    except (ValueError, AttributeError):
                        pass
                    if legacy_callback:
                        on_done(epc, number, ts)
                    else:
                        on_done(result)
                except Exception as exc:
                    log.error(f"RFID on_done callback xatosi: {exc}")

    # ---------------------------------------------------------------
    def status(self) -> dict:
        cache = getattr(self.reader, "_cache", None)
        return {
            "enabled": RFID.enabled,
            "mode": RFID.mode,
            "connected": bool(getattr(self.reader, "connected", False)),
            "state": self._state,
            "last_epc": self._last_epc,
            "last_number": self._last_number,
            "last_ts": self._last_ts.strftime("%Y-%m-%d %H:%M:%S") if self._last_ts else "",
            "read_duration_ms": RFID.read_duration_ms,
            "cache_ingest_count": getattr(cache, "ingest_count", None),
        }
