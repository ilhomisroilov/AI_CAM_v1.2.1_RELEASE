"""
============================================================
plc_service.py  —  PLC polling xizmati (oqim boshqaruvi)
============================================================
Polling:
  * Alohida daemon thread — UI ni HECH QACHON bloklamaydi.
  * Signalni davriy o'qiydi (poll_interval_ms).
  * HOLAT o'zgarishida (level edge) callback chaqiradi:
        signal -> trigger_on_value  =>  on_start()  (ishlov ber)
        signal -> 0                 =>  on_stop()   (to'xta / idle)
  * Ulanish uzilsa eksponensial backoff (1s -> reconnect_max_delay_sec).

MUHIM: PLC FAQAT oqimni boshqaradi. YOLO/PaddleOCR modellari bu yerda
yuklanmaydi/yuklanmaydi — ular ilova ishga tushganda yuklanib, doim xotirada
qoladi (pipeline.warmup_models).
"""
from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Callable, Optional

from ..config import PLC
from ..logger import log
from .plc_base import PLCConfigError, PLCInterface
from .plc_simulator import PLCSimulator


def build_plc() -> PLCInterface:
    """Config (plc.mode) bo'yicha PLC manbasini yaratadi."""
    if PLC.mode == "melsec":
        from .plc_melsec import MelsecPLC
        return MelsecPLC(ip=PLC.ip, port=PLC.port,
                         signal_address=PLC.signal_address, plc_type=PLC.plc_type)
    return PLCSimulator(initial=0)


def validate_plc_config(cfg=None) -> None:
    """Reject an acknowledgement contract that has no approved DONE address."""
    cfg = cfg if cfg is not None else PLC
    if (bool(getattr(cfg, "require_acknowledgement", False))
            and not str(getattr(cfg, "done_address", "") or "").strip()):
        raise PLCConfigError(
            "PLC.require_acknowledgement=true, lekin PLC.done_address sozlanmagan."
        )


class PLCHandshakeState(Enum):
    """Observable PLC-side handshake state; independent from pipeline sessions."""

    IDLE = "IDLE"
    TRIGGER_DETECTED = "TRIGGER_DETECTED"
    BUSY = "BUSY"
    PROCESSING = "PROCESSING"
    RESULT_READY = "RESULT_READY"
    WAITING_ACK = "WAITING_ACK"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ERROR = "ERROR"


class PLCService:
    """PLC signalini kuzatadi va start/stop callbacklarini chaqiradi."""

    def __init__(self,
                 on_start: Callable[[], None],
                 on_stop: Callable[[], None],
                 on_exit: Optional[Callable[[], None]] = None) -> None:
        validate_plc_config()
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_exit = on_exit
        self._last_exit_value: Optional[int] = None
        self.plc: PLCInterface = build_plc()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        # 0 = idle deb boshlanadi -> startup da signal 0 bo'lsa STOP chaqirilmaydi,
        # lekin startup da signal allaqachon 1 bo'lsa START chaqiriladi.
        self._last_signal: Optional[int] = 0   # oxirgi XOM qiymat (status/log uchun)
        # P1 FIX: edge detektsiyasi XOM word emas, hisoblangan HOLAT (0/1) ustidan.
        self._last_state: int = 0
        # P6 FIX: "arm" bayrog'i. Trigger ishlagach disarm bo'ladi; signal FIZIK 0 ga
        # tushgachgina qayta arm bo'ladi -> auto-off dan keyin re-trigger bo'roni yo'q.
        self._armed: bool = True
        self._connected = False
        self._last_read_ts: float = 0.0      # oxirgi muvaffaqiyatli o'qish (last packet)
        self._attempt = 0
        self._hlock = threading.RLock()
        self._hstate = PLCHandshakeState.IDLE
        self._first_poll_done = False
        self._startup_waiting_for_off = False
        self._ack_deadline: Optional[float] = None
        self._ack_timed_out = False
        self._ack_timeout_count = 0
        # One-shot teardown (haqiqiy PLC): VIN/sessiya tugagach kamera o'chirish +
        # handshake DONE yozish poll THREADida bajariladi (OCR threadi bloklanmaydi).
        self._pending_teardown = False
        self._done_written_for_teardown = False

    # ---------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="plc-poll", daemon=True)
        self._thread.start()
        log.info(f"PLC xizmati ishga tushdi (mode={PLC.mode}, "
                 f"signal={PLC.signal_address}, interval={PLC.poll_interval_ms}ms).")

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        try:
            self.plc.close()
        except Exception:
            pass
        log.info("PLC xizmati to'xtatildi.")

    # ---------------------------------------------------------------
    # Simulator boshqaruvi (UI ON/OFF tugmalari)
    # ---------------------------------------------------------------
    def set_sim_state(self, value: int) -> bool:
        if isinstance(self.plc, PLCSimulator):
            self.plc.set_state(value)
            return True
        return False                       # haqiqiy PLC da qo'lda o'rnatib bo'lmaydi

    # ---------------------------------------------------------------
    # ONE-SHOT: VIN o'qilgach signalni 0 ga tushiramiz (kamera o'chadi).
    # Bu pipeline (OCR threadi) dan chaqiriladi — FAQAT yengil bayroq/qiymat
    # o'rnatadi. Haqiqiy teardown (kamera uzish) poll THREADIDA bajariladi,
    # shuning uchun OCR threadi bloklanmaydi.
    # ---------------------------------------------------------------
    def notify_vin_done(self) -> None:
        # Sessiya yakunlanganda chaqiriladi (SUCCESS, qisman yoki timeout) — VIN
        # o'qilganligini ANGLATMAYDI. Log neytral.
        with self._hlock:
            if self._hstate in {
                PLCHandshakeState.BUSY,
                PLCHandshakeState.PROCESSING,
                PLCHandshakeState.TRIGGER_DETECTED,
            }:
                self._hstate = PLCHandshakeState.RESULT_READY

        if isinstance(self.plc, PLCSimulator):
            self.plc.set_state(0)          # signal 0 -> poll keyingi sikl da STOP + re-arm
            log.info("Sessiya yakunlandi -> PLC simulyator signali 0 (kamera o'chadi).")
            with self._hlock:
                self._hstate = PLCHandshakeState.WAITING_ACK
                self._ack_deadline = None
                self._ack_timed_out = False
        else:
            # P6 FIX: lokal 0 sintez QILMAYMIZ (u soxta ko'tarilish qirrasi yaratardi).
            # O'rniga: disarm + handshake DONE + teardown poll threadida bajariladi.
            # Hardened transports may implement write_bit directly and promise a
            # short acknowledgement write. Preserve the current Melsec
            # write_signal path on the poll thread so OCR/finalization cannot be
            # blocked by network I/O.
            if type(self.plc).write_bit is not PLCInterface.write_bit:
                self._write_done()
                self._done_written_for_teardown = True
            self._pending_teardown = True
            with self._hlock:
                self._hstate = PLCHandshakeState.WAITING_ACK
                self._ack_timed_out = False
                if bool(getattr(PLC, "require_acknowledgement", False)):
                    self._ack_deadline = time.monotonic() + (
                        float(getattr(PLC, "ack_timeout_ms", 5000)) / 1000.0
                    )
                else:
                    self._ack_deadline = None
            log.info("Sessiya yakunlandi -> PLC teardown navbatga qo'yildi "
                     "(disarm + DONE handshake, kamera o'chadi).")

    def status(self) -> dict:
        with self._hlock:
            handshake_state = self._hstate.value
            ack_timeout_count = self._ack_timeout_count
        return {
            "enabled": PLC.enabled,
            "running": self._running,
            "mode": PLC.mode,
            "connected": self._connected,
            "signal": self._last_signal,
            "signal_address": PLC.signal_address,
            "is_simulator": isinstance(self.plc, PLCSimulator),
            "last_read_ts": self._last_read_ts,
            "handshake_state": handshake_state,
            "ack_timeout_count": ack_timeout_count,
            "write_enabled": bool(getattr(PLC, "write_enabled", True)),
            "require_acknowledgement": bool(
                getattr(PLC, "require_acknowledgement", False)),
            "done_address": getattr(PLC, "done_address", ""),
        }

    # ---------------------------------------------------------------
    def _loop(self) -> None:
        interval = max(0.05, PLC.poll_interval_ms / 1000.0)
        while not self._stop_event.is_set():
            waited = self._poll_once()
            if waited:
                continue
            self._stop_event.wait(interval)

    def _poll_once(self) -> bool:
        """Execute one network-safe poll iteration; return True after backoff/error."""
        if not self._connected:
            if not self._safe_connect():
                delay = min(1.0 * (2 ** min(self._attempt, 5)),
                            PLC.reconnect_max_delay_sec)
                self._attempt += 1
                self._stop_event.wait(delay)
                return True
            self._attempt = 0
            self._connected = True

        value = self.plc.read_signal()
        if value is None:
            self._connected = False
            return True

        self._last_read_ts = time.time()
        self._last_signal = value
        state = self._compute_trigger_state(value)

        # v1.3.0 forensic edge audit (additive, best-effort — never blocks polling).
        if bool(getattr(PLC, "plc_edge_audit_enabled", True)):
            try:
                if getattr(self, "_edge_auditor", None) is None:
                    from .edge_audit import from_config
                    self._edge_auditor = from_config()
                self._edge_auditor.feed(state)
            except Exception:
                pass

        if not self._first_poll_done:
            self._first_poll_done = True
            if state and str(getattr(
                    PLC, "startup_recovery_policy", "wait_for_edge")).lower() != "start_immediately":
                self._last_state = 1
                self._armed = False
                self._startup_waiting_for_off = True
                with self._hlock:
                    self._hstate = PLCHandshakeState.BUSY
                log.warning("PLC startup: signal ON topildi; yangi edge oldidan OFF kutiladi.")
                self._poll_exit_signal()
                return False

        if self._pending_teardown:
            self._pending_teardown = False
            if not self._done_written_for_teardown:
                self._write_done()
            self._done_written_for_teardown = False
            if str(getattr(PLC, "trigger_mode", "level")).lower() == "pulse":
                # Pulse falling-edge sessionni yopmaydi. Finalization allaqachon
                # pipeline tomonida bajarilgan; keyingi physical edge yo'qolmasin.
                self._handle_transition(value)
            else:
                self._armed = False
                self._last_state = state
                try:
                    self._on_stop()
                except Exception as exc:
                    log.error(f"PLC teardown on_stop xatosi: {exc}")
            self._check_ack_timeout(state)
        else:
            self._check_ack_timeout(state)
            self._handle_transition(value)

        self._poll_exit_signal()
        return False

    def _check_ack_timeout(self, state: int) -> None:
        with self._hlock:
            expired = (
                state == 1
                and self._hstate == PLCHandshakeState.WAITING_ACK
                and bool(getattr(PLC, "require_acknowledgement", False))
                and self._ack_deadline is not None
                and time.monotonic() > self._ack_deadline
                and not self._ack_timed_out
            )
            if not expired:
                return
            self._ack_timed_out = True
            self._ack_timeout_count += 1
            self._hstate = PLCHandshakeState.ERROR
        log.critical("PLC ACK timeout: signal hamon ON; real OFF gacha rearm bloklandi.")

    def _poll_exit_signal(self) -> None:
        address = str(getattr(PLC, "exit_address", "") or "").strip()
        if self._on_exit is None or not address:
            return
        value = self.plc.read_register(address)
        if value is None:
            return
        expected = int(getattr(PLC, "exit_on_value", 1))
        rising = self._last_exit_value != expected and int(value) == expected
        self._last_exit_value = int(value)
        if rising:
            try:
                self._on_exit()
            except Exception as exc:
                log.error(f"PLC on_exit xatosi: {exc}")

    def _safe_connect(self) -> bool:
        try:
            return bool(self.plc.connect())
        except Exception as exc:
            log.error(f"PLC connect xatosi: {exc}")
            return False

    def _compute_trigger_state(self, value: int) -> int:
        """
        P1 FIX: XOM register qiymatidan (word bo'lishi mumkin) trigger HOLATINI
        (0/1) hisoblaydi. signal_kind ga ko'ra:
          value -> butun qiymat == trigger_on_value (haqiqiy BIT qurilma uchun)
          bit   -> word ichidan signal_bit indeksli bit
          mask  -> (value & trigger_mask) == (trigger_on_value & trigger_mask)
        """
        try:
            v = int(value)
        except Exception:
            return 0
        legacy_kind = getattr(PLC, "trigger_kind", None)
        if legacy_kind == "bit_or_mask":
            bit = int(getattr(PLC, "trigger_bit", 0))
            return 1 if ((v >> bit) & 1) else 0
        if legacy_kind == "level":
            return 1 if v == int(PLC.trigger_on_value) else 0
        kind = (getattr(PLC, "signal_kind", "auto") or "auto").lower()
        bit = int(getattr(PLC, "signal_bit", -1))
        mask = int(getattr(PLC, "trigger_mask", 0))
        if kind == "auto":
            if mask:
                kind = "mask"
            elif bit >= 0:
                kind = "bit"
            else:
                kind = "value"
        if kind == "bit" and bit >= 0:
            return 1 if ((v >> bit) & 1) else 0
        if kind == "mask" and mask:
            return 1 if (v & mask) == (int(PLC.trigger_on_value) & mask) else 0
        # value rejimi (default) — eski xulq (BIT qurilma 0/1 qaytaradi)
        return 1 if v == int(PLC.trigger_on_value) else 0

    def _write_done(self) -> None:
        """P6 FIX: handshake — PLC ga DONE bitini yozadi (ARRIVE ni 0 ga tushirish uchun)."""
        addr = (getattr(PLC, "done_address", "") or "").strip()
        if not addr or not bool(getattr(PLC, "write_enabled", True)):
            return
        try:
            ok = self.plc.write_bit(addr, int(getattr(PLC, "done_value", 1)))
            if ok:
                log.info(f"PLC handshake: DONE -> {addr}={PLC.done_value} (ARRIVE ni kutamiz).")
        except Exception as exc:
            log.warning(f"PLC DONE handshake xatosi: {exc}")

    def _handle_transition(self, value: int) -> None:
        """
        Holat (0/1) qirrasi bo'yicha start/stop. P1: word emas, hisoblangan holat.
        P6: arm bayrog'i — trigger ishlagach disarm; signal 0 ga tushgachgina re-arm.
        """
        state = self._compute_trigger_state(value)
        if state == self._last_state:
            return                              # holat o'zgarmadi -> hech narsa
        self._last_state = state
        # trigger_kind is the legacy hardening contract and explicitly models
        # level semantics. The current production profile leaves it None and
        # uses trigger_mode=pulse, whose falling edge must not close a session.
        legacy_kind = getattr(PLC, "trigger_kind", None)
        pulse = (legacy_kind is None
                 and str(getattr(PLC, "trigger_mode", "level")).lower() == "pulse")

        if state == 1:                          # ko'tarilish qirrasi (ARRIVE)
            if pulse:
                # PULSE rejimi: har ko'tarilish = yangi mashina. Arm/disarm o'yini
                # YO'Q (signal tabiiy 0 ga tushadi). plc_on faol sessiyani himoya qiladi.
                log.info(f"PLC trigger (raw={value}, pulse) -> ishlov berish BOSHLANADI.")
                try:
                    with self._hlock:
                        self._hstate = PLCHandshakeState.BUSY
                    self._on_start()
                except Exception as exc:
                    log.error(f"PLC on_start xatosi: {exc}")
            elif self._armed or not getattr(PLC, "require_zero_before_rearm", True):
                self._armed = False             # bu triggerni "iste'mol" qildik
                log.info(f"PLC trigger (raw={value}) -> ishlov berish BOSHLANADI.")
                try:
                    with self._hlock:
                        self._hstate = PLCHandshakeState.BUSY
                    self._on_start()
                except Exception as exc:
                    log.error(f"PLC on_start xatosi: {exc}")
            else:
                log.info(f"PLC ko'tarilish qirrasi (raw={value}) e'tiborsiz — disarmed "
                         f"(signal 0 ga tushishini kutmoqda).")
        else:                                   # tushish qirrasi (signal 0)
            self._armed = True                  # fizik 0 -> qayta arm
            if pulse:
                # PULSE: 0 ga tushish sessiyani YOPMAYDI. Sessiya VIN o'qilguncha yoki
                # timeout gacha davom etadi; kamera VIN/timeout dan keyin auto-off bilan o'chadi.
                log.info(f"PLC signal 0 (raw={value}, pulse) -> puls tugadi; "
                         f"sessiya davom etadi (VIN yoki timeout gacha).")
                with self._hlock:
                    finalized = self._hstate in {
                        PLCHandshakeState.RESULT_READY,
                        PLCHandshakeState.WAITING_ACK,
                        PLCHandshakeState.ERROR,
                    }
                    startup_rearmed = self._startup_waiting_for_off
                    if finalized or startup_rearmed:
                        self._hstate = PLCHandshakeState.IDLE
                        self._ack_deadline = None
                        self._ack_timed_out = False
                        self._startup_waiting_for_off = False
                if finalized:
                    try:
                        self._on_stop()
                    except Exception as exc:
                        log.error(f"PLC finalized pulse on_stop xatosi: {exc}")
            else:
                log.info(f"PLC signal 0 (raw={value}) -> ishlov berish TO'XTAYDI (idle, re-armed).")
                with self._hlock:
                    self._hstate = PLCHandshakeState.IDLE
                    self._ack_deadline = None
                    self._ack_timed_out = False
                try:
                    self._on_stop()
                except Exception as exc:
                    log.error(f"PLC on_stop xatosi: {exc}")
