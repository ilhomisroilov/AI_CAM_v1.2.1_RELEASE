"""
============================================================
tag_cache.py  —  R700 oqimidan kelgan teglar keshi
============================================================
gateway_python: rfid_plc_gateway/core/tag_cache.py + models.TagHit dan
ko'chirilgan. Oqim tinglovchi (R700StreamListener) teglarni shu yerga
yozadi; o'qish oynasida ENG YAXShI (eng kuchli RSSI) teg tanlanadi.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional


@dataclass
class TagHit:
    epc: str
    antenna: int
    rssi: Optional[float]
    timestamp: datetime


class TagCache:
    """Teglar keshi (thread-safe). gateway TagCache bilan bir xil semantika."""

    def __init__(self) -> None:
        self._tags: Dict[str, TagHit] = {}
        self._ingest_count = 0
        self._sync = threading.Lock()

    @property
    def ingest_count(self) -> int:
        with self._sync:
            return self._ingest_count

    def upsert(self, hit: TagHit) -> None:
        if hit is None or not (hit.epc or "").strip():
            return
        with self._sync:
            self._tags[hit.epc] = hit
            self._ingest_count += 1
            if self._ingest_count % 200 == 0:
                self._prune_old_locked(60)

    def get_best_in_window(
        self,
        start_utc: datetime,
        end_utc: datetime,
        epc_ok: Optional[Callable[[str], bool]] = None,
    ) -> Optional[TagHit]:
        """Oynadagi (start..end) eng kuchli RSSI ga ega tegni qaytaradi."""
        best: Optional[TagHit] = None
        with self._sync:
            vals = list(self._tags.values())
        for hit in vals:
            if hit.timestamp < start_utc or hit.timestamp > end_utc:
                continue
            if epc_ok is not None and not epc_ok(hit.epc or ""):
                continue
            this_rssi = hit.rssi if hit.rssi is not None else float("-inf")
            best_rssi = best.rssi if (best is not None and best.rssi is not None) else float("-inf")
            if best is None or this_rssi > best_rssi:
                best = hit
        return best

    def prune_old(self, max_age_seconds: int = 60) -> None:
        with self._sync:
            self._prune_old_locked(max_age_seconds)

    def _prune_old_locked(self, max_age_seconds: int) -> None:
        cutoff = datetime.utcnow() - timedelta(seconds=max_age_seconds)
        to_remove = [k for k, v in self._tags.items() if v.timestamp < cutoff]
        for key in to_remove:
            self._tags.pop(key, None)
