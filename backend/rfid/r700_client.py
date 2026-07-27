"""
============================================================
r700_client.py  —  Impinj R700 REST mijozi
============================================================
gateway_python: rfid_plc_gateway/r700/r700_rest_client.py dan ko'chirilgan.
Farqi: IP/port/timeout/auth endi HARDCODE EMAS — config.yaml (RFID) dan keladi.

`requests` faqat shu yerda kerak (R700 rejimi). Simulator rejimida import
qilinmaydi (lazy import) — shuning uchun requests o'rnatilmagan bo'lsa ham
ilova simulator bilan ishlayveradi.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..logger import log


class R700RestClient:
    """R700 REST API (inventory profillarini boshqarish)."""

    def __init__(self, ip: str, port: int = 80, username: str = "root",
                 password: str = "", timeout_s: float = 5.0) -> None:
        if not ip:
            raise RuntimeError("R700 IP manzili kerak")
        import requests  # lazy — faqat R700 rejimida
        host = ip if (port in (80, 0)) else f"{ip}:{int(port)}"
        self._base = f"http://{host}/api/v1"
        self._timeout = float(timeout_s)
        self._session = requests.Session()
        self._session.auth = (username or "", password or "")
        self._session.headers.update({"Accept": "application/json"})

    def _request(self, method: str, path: str, timeout_s: float, json_body: Optional[Dict] = None):
        url = f"{self._base}/{path.lstrip('/')}"
        resp = self._session.request(method, url, timeout=timeout_s, json=json_body)
        if not resp.ok:
            body = ""
            try:
                body = resp.text
            except Exception:
                pass
            raise RuntimeError(f"R700 {path} xato: {resp.status_code} {resp.reason}. Body={body}")
        return resp

    def stop_all_profiles(self) -> None:
        self._request("POST", "profiles/stop", timeout_s=3.0, json_body={})

    # ---- gateway: preset (profil) boshqaruvi — VERBATIM mantiq ----
    def get_inventory_presets(self) -> List[Any]:
        """Mavjud inventory presetlar ro'yxati (firmware variantlariga bardoshli)."""
        payload = self._request("GET", "profiles/inventory/presets", timeout_s=5.0).json()
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("presets", "items", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    def get_inventory_preset_detail(self, preset_id: str) -> Optional[Dict[str, Any]]:
        """Bitta presetning to'liq konfiguratsiyasi (antenna/power ni o'zgartirish uchun)."""
        if not preset_id:
            return None
        import requests
        return self._request(
            "GET", f"profiles/inventory/presets/{requests.utils.quote(preset_id)}", timeout_s=5.0
        ).json()

    def put_inventory_preset(self, preset_id: str, preset_detail: Dict[str, Any]) -> None:
        """Presetni (gateway konfiguratsiyasi bilan) saqlaydi/yangilaydi."""
        if not preset_id:
            raise RuntimeError("preset_id kerak")
        import requests
        self._request(
            "PUT", f"profiles/inventory/presets/{requests.utils.quote(preset_id)}",
            timeout_s=8.0, json_body=preset_detail
        )

    def start_inventory_preset(self, preset_id: str) -> None:
        """Saqlangan presetni ishga tushiradi (transient o'rniga — gateway afzal yo'li)."""
        if not preset_id:
            raise RuntimeError("preset_id kerak")
        import requests
        path = f"profiles/inventory/presets/{requests.utils.quote(preset_id)}/start"
        try:
            self._request("POST", path, timeout_s=8.0, json_body={})
            return
        except RuntimeError as exc:
            if "409" not in str(exc) and "Conflict" not in str(exc):
                raise
            try:
                self.stop_all_profiles()
            except Exception as stop_exc:
                log.warning(f"RFID: stop_all_profiles xato (409 dan keyin): {stop_exc}")
            time.sleep(0.2)
            self._request("POST", path, timeout_s=8.0, json_body={})

    def start_inventory_transient(self, tx_power_cdbm: Optional[int],
                                  antenna_ports: Optional[List[int]] = None) -> None:
        """Inventory ni darhol boshlaydi (preset shart emas). 409 -> stop + qayta urinish."""
        import requests
        body = self._build_minimal_inventory_start_request(tx_power_cdbm, antenna_ports)
        url = f"{self._base}/profiles/inventory/start"
        resp = self._session.post(url, timeout=8.0, json=body)
        if resp.ok:
            return
        if resp.status_code == 409:                  # allaqachon ishlayapti -> to'xtatib qayta
            try:
                self.stop_all_profiles()
            except Exception as exc:
                log.warning(f"RFID: stop_all_profiles xato (409 dan keyin): {exc}")
            time.sleep(0.2)
            resp2 = self._session.post(url, timeout=8.0, json=body)
            if resp2.ok:
                return
            raise RuntimeError(f"R700 transient inventory start xato: "
                               f"{resp2.status_code} {resp2.reason}. Body={resp2.text}")
        raise RuntimeError(f"R700 transient inventory start xato: "
                           f"{resp.status_code} {resp.reason}. Body={resp.text}")

    # ---- gateway: _build_minimal_inventory_start_request — VERBATIM mantiq ----
    @staticmethod
    def _build_minimal_inventory_start_request(
        tx_power_cdbm: Optional[int], antenna_ports: Optional[List[int]]
    ) -> Dict[str, Any]:
        pwr = R700RestClient._normalize_and_clamp_cdbm(tx_power_cdbm if tx_power_cdbm is not None else 3150)
        ports = R700RestClient._normalize_antenna_ports(antenna_ports)
        cfgs = []
        for port in ports:
            cfgs.append({
                "antennaPort": port,
                "transmitPowerCdbm": pwr,
                "rfMode": 1002,
                "inventorySession": 2,
                "inventorySearchMode": "dual-target",
                "estimatedTagPopulation": 32,
            })
        return {"antennaConfigs": cfgs}

    @staticmethod
    def _normalize_and_clamp_cdbm(cdbm: int) -> int:
        v = int(cdbm)
        if 0 < v < 400:
            v *= 100
        if v < 0:
            v = 0
        if v > 3150:
            v = 3150
        return v

    @staticmethod
    def _normalize_antenna_ports(antenna_ports: Optional[List[int]]) -> List[int]:
        default_ports = [1, 2, 3, 4]
        if not antenna_ports:
            return default_ports
        result: List[int] = []
        for p in antenna_ports:
            try:
                v = int(p)
            except Exception:
                continue
            if 1 <= v <= 4 and v not in result:
                result.append(v)
        return result or default_ports
