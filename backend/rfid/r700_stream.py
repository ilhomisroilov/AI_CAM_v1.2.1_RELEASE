"""
============================================================
r700_stream.py  —  Impinj R700 SSE oqim tinglovchi
============================================================
gateway_python: rfid_plc_gateway/r700/r700_stream_listener.py dan ko'chirilgan.
R700 `/api/v1/data/stream` (Server-Sent Events) orqali teg hodisalarini
uzluksiz push qiladi. Har bir "tagInventory" hodisasidan EPC ajratiladi,
normallashtiriladi va TagCache ga yoziladi.

Qayta ulanish (reconnect) run_forever ichida: ulanish uzilsa 1s kutib qayta
ulanadi (stop_event qo'yilmaguncha). IP/port/auth config dan keladi.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Optional

from ..logger import log
from .tag_cache import TagCache, TagHit


class R700StreamListener:
    def __init__(self, reader_ip: str, cache: TagCache, port: int = 80,
                 username: str = "", password: str = "",
                 connect_timeout_s: float = 5.0) -> None:
        if not reader_ip:
            raise RuntimeError("reader_ip kerak")
        if cache is None:
            raise RuntimeError("cache kerak")
        import requests  # lazy
        self._cache = cache
        host = reader_ip if (port in (80, 0)) else f"{reader_ip}:{int(port)}"
        self._url = f"http://{host}/api/v1/data/stream"
        self._connect_timeout = float(connect_timeout_s)
        self._connected = False
        self._session = requests.Session()
        self._session.auth = (username or "", password or "")
        self._session.headers.update({"Accept": "text/event-stream"})
        self._seen_count = 0

    @property
    def connected(self) -> bool:
        return self._connected

    def run_forever(self, stop_event) -> None:
        """Uzluksiz tinglaydi; uzilsa 1s kutib qayta ulanadi (avto-reconnect)."""
        while not stop_event.is_set():
            try:
                self._run_once(stop_event)
            except Exception as exc:
                self._connected = False
                log.warning(f"RFID oqim xatosi (qayta ulanadi): {exc}")
            if stop_event.wait(1.0):
                return

    def _run_once(self, stop_event) -> None:
        with self._session.get(self._url, stream=True,
                               timeout=(self._connect_timeout, None)) as resp:
            resp.raise_for_status()
            self._connected = True
            log.info("RFID R700 oqimi ulandi (data/stream).")
            multi_line_buffer = []
            for raw in resp.iter_lines(decode_unicode=True):
                if stop_event.is_set():
                    return
                if raw is None:
                    continue
                line = raw.strip()
                if not line:
                    if multi_line_buffer:
                        self._process_possible_json("\n".join(multi_line_buffer))
                        multi_line_buffer = []
                    continue
                low = line.lower()
                if (low.startswith("event:") or low.startswith("id:")
                        or low.startswith("retry:") or line.startswith(":")):
                    continue
                if low.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                if self._looks_like_json_object(line):
                    self._process_possible_json(line)
                    continue
                multi_line_buffer.append(line)
                buf = "\n".join(multi_line_buffer)
                if self._looks_like_json_object(buf):
                    self._process_possible_json(buf)
                    multi_line_buffer = []

    @staticmethod
    def _looks_like_json_object(s: str) -> bool:
        if not s:
            return False
        first = s.find("{")
        last = s.rfind("}")
        return first >= 0 and last > first

    def _process_possible_json(self, data: str) -> None:
        first = data.find("{")
        last = data.rfind("}")
        if first < 0 or last <= first:
            return
        json_text = data[first:last + 1]
        try:
            obj = json.loads(json_text)
            event_type = (obj.get("eventType") or "").strip()
            if event_type.lower() != "taginventory":
                return
            t = obj.get("tagInventoryEvent")
            if not isinstance(t, dict):
                return
            epc = self._normalize_epc(self._get_epc_hex(t))
            if not epc or epc == "<no-epc>":
                return
            rssi = None
            if t.get("peakRssiCdbm") is not None:
                rssi = int(t.get("peakRssiCdbm")) / 100.0
            elif t.get("rssi") is not None:
                rssi = float(t.get("rssi"))
            antenna = int(t.get("antennaPort") or t.get("antenna") or 0)
            self._cache.upsert(TagHit(epc=epc, antenna=antenna, rssi=rssi, timestamp=datetime.utcnow()))
            self._seen_count += 1
            if self._seen_count <= 5 or self._seen_count % 100 == 0:
                log.info(f"RFID tag event: EPC={epc} antenna={antenna} rssi={rssi}")
        except Exception:
            pass

    # ---- gateway EPC ajratish/normalizatsiya — VERBATIM ----
    @classmethod
    def _get_epc_hex(cls, tag_node: dict) -> str:
        epc = tag_node.get("epc")
        if epc is None and isinstance(tag_node.get("tag"), dict):
            epc = tag_node["tag"].get("epc")
        s = (epc or "").strip()
        if not s:
            return "<no-epc>"
        if s.isdigit():
            return s
        if cls._looks_like_hex(s):
            return s
        if cls._has_non_digit(s):
            decoded = cls._try_decode_base64url(s)
            if decoded is not None:
                try:
                    txt = decoded.decode("ascii").strip()
                    if txt and all(32 <= ord(c) <= 126 for c in txt):
                        return txt
                except Exception:
                    pass
                return decoded.hex()
        return "<no-epc>"

    @staticmethod
    def _looks_like_hex(s: str) -> bool:
        t = (s or "").strip()
        if not t or len(t) % 2 != 0:
            return False
        return all(c in "0123456789abcdefABCDEF" for c in t)

    @staticmethod
    def _has_non_digit(s: str) -> bool:
        for c in s:
            if c < "0" or c > "9":
                return True
        return False

    @staticmethod
    def _normalize_epc(epc: str) -> str:
        s = (epc or "").strip().lstrip("0")
        return s if s else "0"

    @staticmethod
    def _try_decode_base64url(s: str) -> Optional[bytes]:
        if not s:
            return None
        normalized = s.replace("-", "+").replace("_", "/")
        rem = len(normalized) % 4
        if rem == 2:
            normalized += "=="
        elif rem == 3:
            normalized += "="
        try:
            return base64.b64decode(normalized.encode("ascii"))
        except Exception:
            return None
