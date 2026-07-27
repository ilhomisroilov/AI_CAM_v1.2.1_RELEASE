"""
============================================================
rfid_reader.py  —  Yuqori darajali RFID o'qigich (R700 + Simulator)
============================================================
gateway_python ArriveProcessor o'qish oqimini soddalashtirib takrorlaydi:
  PLC=1 -> start_inventory_transient -> read_duration_ms davomida oqimdan
  teglar yig'iladi (TagCache) -> oynadagi eng yaxshi (RSSI) teg tanlanadi ->
  stop_all_profiles. EPC qaytariladi (None bo'lsa teg o'qilmadi).

RFIDReaderBase:
  start()      — fon oqim tinglovchini ishga tushiradi (R700) / no-op (sim)
  read_tag(stop_event) -> Optional[str]  (xom EPC, normalizatsiyalangan)
  stop()       — to'xtatadi
  connected    — ulanish holati
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Optional

from ..config import RFID
from ..logger import log
from .epc_extract import extract_rfid_number, is_factory_epc_decimal
from .tag_cache import TagCache


class RFIDSimulator:
    """
    Hardware bo'lmasdan oqimni sinash uchun (PLC simulyatoriga mos).

    MUHIM (RFID integrity): simulyator HECH QACHON qiymat O'YLAB TOPMAYDI.
    Faqat operator config da ANIQ bergan EPC (rfid.sim_epc) qaytariladi. U bo'sh
    bo'lsa -> None (teg yo'q = NO_TAG). Tasodifiy/taxminiy raqam GENERATSIYA
    QILINMAYDI — faqat haqiqiy/aniq kiritilgan ma'lumot.
    """

    def __init__(self) -> None:
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        epc = (RFID.sim_epc or "").strip()
        if epc:
            log.info(f"RFID simulyatori tayyor (mode=simulator, aniq EPC='{epc}').")
        else:
            log.info("RFID simulyatori tayyor (mode=simulator, sim_epc bo'sh -> NO_TAG).")

    def read_tag(self, stop_event=None, duration_ms: Optional[int] = None,
                 deadline: Optional[float] = None) -> Optional[str]:
        # O'qish oynasining kichik qismini kutamiz (realistik kechikish)
        dur_ms = duration_ms if duration_ms is not None else RFID.inventory_duration_ms
        dur = max(0.0, min(int(dur_ms), 400) / 1000.0)
        if stop_event is not None and stop_event.wait(dur):
            return None
        # FAQAT operator aniq bergan EPC. Bo'sh -> teg yo'q (None). O'YLAB TOPILMAYDI.
        epc = (RFID.sim_epc or "").strip()
        return epc if epc else None

    def stop(self) -> None:
        self._connected = False


class R700Reader:
    """Impinj R700 (REST + SSE oqim). gateway logikasini takrorlaydi."""

    GATEWAY_PRESET_ID = "gateway"

    def __init__(self) -> None:
        from .r700_client import R700RestClient
        from .r700_stream import R700StreamListener
        self._cache = TagCache()
        self._rest = R700RestClient(
            ip=RFID.ip_address, port=RFID.port,
            username=RFID.username, password=RFID.password, timeout_s=RFID.timeout,
        )
        self._listener = R700StreamListener(
            reader_ip=RFID.ip_address, cache=self._cache, port=RFID.port,
            username=RFID.username, password=RFID.password, connect_timeout_s=RFID.timeout,
        )
        self._stream_stop = threading.Event()
        self._stream_thread: Optional[threading.Thread] = None
        # gateway: ishga tushganda "gateway" preset sozlanadi; o'qishda shu preset
        # ishlatiladi (transient — faqat fallback). None bo'lsa transient.
        self._preset_to_start: Optional[str] = None
        self._antenna_ports = self._normalize_antenna_ports(list(RFID.antenna_ports))

    @property
    def connected(self) -> bool:
        return self._listener.connected

    def start(self) -> None:
        """Fon SSE oqim tinglovchini ishga tushiradi (avto-reconnect ichida)."""
        if self._stream_thread:
            return
        self._stream_stop.clear()
        self._stream_thread = threading.Thread(
            target=self._listener.run_forever, args=(self._stream_stop,),
            name="rfid-stream", daemon=True,
        )
        self._stream_thread.start()
        log.info(f"RFID R700 oqim tinglovchisi ishga tushdi ({RFID.ip_address}:{RFID.port}).")
        # gateway ArriveProcessor.initialize() — "gateway" presetni sozlaymiz
        try:
            self.initialize()
        except Exception as exc:
            log.warning(f"RFID R700 initialize xatosi (transient ishlatiladi): {exc}")
            self._preset_to_start = None

    def initialize(self) -> None:
        """
        gateway ArriveProcessor.initialize() AYNAN takrori:
          * mavjud profillarni to'xtatadi,
          * bazaviy presetni tanlaydi (gateway/default/birinchi),
          * uning konfiguratsiyasini oladi, antenna portlari + tx_power ni qo'llaydi,
          * "gateway" presetiga saqlaydi va o'qishda shu presetni ishlatadi.
        Preset topilmasa/sozlanmasa -> transient (None) ga qaytadi.
        """
        try:
            self._rest.stop_all_profiles()
        except Exception as exc:
            log.warning(f"RFID R700: stop_all_profiles (init) xato: {exc}")
        self._preset_to_start = None
        presets = self._rest.get_inventory_presets()
        base_preset = self._pick_base_preset_id(presets)
        if not base_preset:
            log.info("RFID R700: preset topilmadi — transient inventory ishlatiladi.")
            self._preset_to_start = None
            return
        detail = self._rest.get_inventory_preset_detail(base_preset)
        if not detail:
            self._preset_to_start = base_preset
            log.info(f"RFID R700: '{base_preset}' preseti to'g'ridan-to'g'ri ishlatiladi.")
            return
        self._ensure_antenna_configs_for_ports(detail, self._antenna_ports)
        if RFID.tx_power_cdbm is not None:
            self._apply_transmit_power_cdbm(detail, int(RFID.tx_power_cdbm))
        self._rest.put_inventory_preset(self.GATEWAY_PRESET_ID, detail)
        self._preset_to_start = self.GATEWAY_PRESET_ID
        log.info(f"RFID R700: '{self.GATEWAY_PRESET_ID}' preset sozlandi "
                 f"(portlar={self._antenna_ports}, tx_power_cdbm={RFID.tx_power_cdbm}).")

    def read_tag(self, stop_event=None, duration_ms: Optional[int] = None,
                 deadline: Optional[float] = None) -> Optional[str]:
        """
        BITTA inventory urinishi (gateway _run_inventory_attempt AYNAN takrori):
          1) inventory boshlanadi (preset bo'lsa preset, aks holda transient),
          2) reader "isishi" uchun warmup_ms kutiladi (gateway: 0.15s),
          3) duration_ms davomida oqimdan teglar keshga yig'iladi,
          4) oynadagi eng yaxshi (RSSI) teg tanlanadi,
          5) inventory to'xtatiladi.
        `deadline` (monotonic) berilgan bo'lsa, oyna shu vaqtdan oshmaydi.
        Qayta urinish (retry) sikli RFIDService da — bu metod faqat 1 urinish.
        """
        import time as _t
        dur_ms = int(duration_ms if duration_ms is not None else RFID.inventory_duration_ms)
        warmup_s = max(0.0, int(RFID.warmup_ms) / 1000.0)
        started = False
        try:
            if self._preset_to_start:
                self._rest.start_inventory_preset(self._preset_to_start)
            else:
                self._rest.start_inventory_transient(RFID.tx_power_cdbm, list(self._antenna_ports))
            started = True

            # 0.15s warmup — reader/antenna teg energiyalashi uchun (gateway bilan bir xil)
            if warmup_s > 0:
                if stop_event is not None and stop_event.wait(warmup_s):
                    return None
                if stop_event is None:
                    _t.sleep(warmup_s)

            # Oyna davomiyligini deadline bilan cheklaymiz (sessiya timeoutidan oshmaslik)
            dur_s = max(0.0, dur_ms / 1000.0)
            if deadline is not None:
                remaining = deadline - _t.monotonic()
                if remaining <= 0:
                    return None
                dur_s = min(dur_s, remaining)

            window_start = datetime.utcnow() - timedelta(milliseconds=250)
            if stop_event is not None and stop_event.wait(dur_s):
                return None
            elif stop_event is None:
                _t.sleep(dur_s)
            window_end = datetime.utcnow()

            epc_ok = None
            if RFID.epc_decimal_validate:
                epc_ok = self._factory_epc_ok
            hit = self._cache.get_best_in_window(window_start, window_end, epc_ok)
            return hit.epc if hit is not None else None
        finally:
            if started:
                try:
                    self._rest.stop_all_profiles()
                except Exception as exc:
                    log.warning(f"RFID: stop_all_profiles xato: {exc}")

    # ---- gateway ArriveProcessor preset yordamchilari — VERBATIM mantiq ----
    @staticmethod
    def _factory_epc_ok(epc: str) -> bool:
        lo, hi = int(RFID.epc_decimal_min), int(RFID.epc_decimal_max)
        if is_factory_epc_decimal(epc, lo, hi):
            return True
        number = extract_rfid_number(epc, lo, hi)
        if not (number or "").isdigit():
            return False
        try:
            return lo <= int(number, 10) <= hi
        except Exception:
            return False

    @staticmethod
    def _pick_base_preset_id(presets: list) -> Optional[str]:
        if not presets:
            return None
        ids: list = []
        for item in presets:
            if isinstance(item, str):
                pid = item.strip()
                if pid:
                    ids.append(pid)
                continue
            if isinstance(item, dict):
                pid = str(item.get("id") or "").strip()
                if pid:
                    ids.append(pid)
        if not ids:
            return None
        by_lower = {x.lower(): x for x in ids}
        if "gateway" in by_lower:
            return by_lower["gateway"]
        if "default" in by_lower:
            return by_lower["default"]
        return ids[0]

    @staticmethod
    def _normalize_antenna_ports(antenna_ports: list) -> list:
        result: list = []
        for p in antenna_ports or []:
            try:
                v = int(p)
            except Exception:
                continue
            if 1 <= v <= 4 and v not in result:
                result.append(v)
        return result or [1, 2, 3, 4]

    @staticmethod
    def _ensure_antenna_configs_for_ports(preset_detail: dict, ports: list) -> None:
        """
        Kerakli portlar uchun antenna konfiguratsiyalarini ta'minlaydi.

        MUHIM (R700 talabi): rfMode (va boshqa RF parametrlar) BARCHA antennalarda
        BIR XIL bo'lishi shart — aks holda PUT 400 qaytaradi
        ("#/antennaConfigs/N/rfMode: Must be the same for all antennas").
        Shuning uchun preset dagi MAVJUD birinchi antenna qiymatlarini olamiz
        (reader profili/regioniga mos), bo'lmasa standartga tushamiz, va shu YAGONA
        qiymatlarni hamma antennaga (mavjud + yangi qo'shilgan) qo'llaymiz.
        """
        cfgs = preset_detail.get("antennaConfigs")
        if not isinstance(cfgs, list):
            cfgs = []
            preset_detail["antennaConfigs"] = cfgs
        existing = [c for c in cfgs if isinstance(c, dict)]
        base = existing[0] if existing else {}

        def pick(key, default):
            v = base.get(key)
            return v if v is not None else default

        rf_mode = pick("rfMode", 1002)
        inv_session = pick("inventorySession", 2)
        search_mode = pick("inventorySearchMode", "dual-target")
        tag_pop = pick("estimatedTagPopulation", 32)

        # Mavjud antennalarda rfMode (va shared parametrlar) ni YAGONA qiymatga keltiramiz
        for c in existing:
            c["rfMode"] = rf_mode
            c["inventorySession"] = inv_session
            c["inventorySearchMode"] = search_mode
            c["estimatedTagPopulation"] = tag_pop

        # Yetishmaydigan portlarni SHU shared qiymatlar bilan qo'shamiz
        for p in ports:
            if any(int((x or {}).get("antennaPort") or 0) == p for x in cfgs if isinstance(x, dict)):
                continue
            cfgs.append({
                "antennaPort": p,
                "transmitPowerCdbm": pick("transmitPowerCdbm", 3150),
                "rfMode": rf_mode,
                "inventorySession": inv_session,
                "inventorySearchMode": search_mode,
                "estimatedTagPopulation": tag_pop,
            })

    @staticmethod
    def _apply_transmit_power_cdbm(preset_detail: dict, tx_power_cdbm: int) -> None:
        cfgs = preset_detail.get("antennaConfigs")
        if not isinstance(cfgs, list):
            return
        for c in cfgs:
            if isinstance(c, dict):
                c["transmitPowerCdbm"] = tx_power_cdbm

    def stop(self) -> None:
        self._stream_stop.set()
        if self._stream_thread:
            self._stream_thread.join(timeout=3.0)
            self._stream_thread = None
        try:
            self._rest.stop_all_profiles()
        except Exception:
            pass


def build_reader():
    """Config (rfid.mode) bo'yicha o'qigich yaratadi."""
    if (RFID.mode or "simulator").lower() == "r700":
        return R700Reader()
    return RFIDSimulator()
