"""
============================================================
epc_extract.py  —  EPC -> RFID raqami (gateway_python bilan TO'LIQ mos)
============================================================
Bu modul YANGI algoritm O'YLAB TOPMAYDI. gateway_python da allaqachon
ishlatilgan logika AYNAN ko'chirilgan:

  1) is_factory_epc_decimal(s, lo, hi)
     gateway: rfid_plc_gateway/core/epc_decimal.py — VERBATIM ko'chirma.
     EPC normalizatsiyadan keyin (faqat raqamlar) [lo, hi] oralig'ida bo'lsa
     "zavod tegi" deb hisoblanadi (odatda 1000..9999).

  2) tag_number = epc.lstrip('0')   (bo'sh bo'lsa "0")
     gateway: ArriveProcessor._handle_arrive da DB ga yuboriladigan qiymat:
         tag_number = (hit.epc or "").lstrip("0")
         if not tag_number: tag_number = "0"
     Ya'ni "RFID raqami" = EPC dan boshidagi nollar olib tashlangan qiymat.
     Zavod (o'nlik) teglari uchun bu 1000..9999 oralig'idagi 4 xonali son beradi
     ("EPC ning oxirgi 4 raqami" — foydalanuvchi ta'rifi).

EPC oqimdan KELGANDA gateway R700StreamListener._normalize_epc orqali
boshidagi nollarni oladi (lstrip('0')). Shu sabab bu yerda ham xuddi shu
normalizatsiya qo'llanadi.
"""
from __future__ import annotations

from typing import Optional


# ---- gateway: core/epc_decimal.py — VERBATIM ----
def is_factory_epc_decimal(s: Optional[str], lo: int, hi: int) -> bool:
    """Return True if *s* is non-empty ASCII digits only and int(s) is in [lo, hi] inclusive."""
    if s is None:
        return False
    t = (s or "").strip()
    if not t or not t.isdigit():
        return False
    try:
        n = int(t, 10)
    except Exception:
        return False
    return lo <= n <= hi


# ---- gateway: R700StreamListener._normalize_epc — VERBATIM ----
def normalize_epc(epc: Optional[str]) -> str:
    """gateway normalizatsiyasi: trim + boshidagi nollarni olib tashlash."""
    s = (epc or "").strip().lstrip("0")
    return s if s else "0"


def extract_rfid_number(epc: Optional[str], lo: int = 1000, hi: int = 9999) -> str:
    """
    EPC -> RFID raqami (gateway ArriveProcessor logikasi).

    Asosiy yo'l (gateway bilan AYNAN bir xil):
        tag_number = epc.lstrip('0')   (bo'sh bo'lsa "0")
    Zavod o'nlik teglari uchun bu to'g'ridan-to'g'ri 1000..9999 sonini beradi.

    Moslik (compatibility) yo'li: agar EPC o'nlik bo'lmasa (masalan SGTIN/hex
    EPC: 300833B2DDD9014000001234), gateway validatsiyasi (is_factory_epc_decimal)
    o'tmaydi. Bunday holda hujjatlashtirilgan xatti-harakatni qaytarish uchun
    EPC ichidagi raqamlarning OXIRGI 4 tasi olinadi (300833...001234 -> 1234).
    Bu real (o'nlik) teglar uchun natijani O'ZGARTIRMAYDI — faqat hex EPC larda
    foydalanuvchi misolini aniq qaytaradi.
    """
    raw = (epc or "").strip()
    if not raw:
        return ""
    # 1) gateway-EXACT: tag_number = epc.lstrip('0') (bo'sh -> "0").
    #    O'nlik (raqamli) EPC uchun bu AYNAN gateway natijasini beradi — diapazon
    #    (1000..9999) validatsiyasi teg TANLASHDA qo'llanadi (read_tag/epc_ok),
    #    raqam ajratishda emas. (lo/hi shu sabab bu yerda ishlatilmaydi.)
    stripped = raw.lstrip("0") or "0"
    if stripped.isdigit():
        return stripped

    # 2) moslik: EPC o'nlik bo'lmasa (hex/SGTIN, masalan 300833B2DDD9014000001234),
    #    hujjatdagi xatti-harakatni qaytarish uchun ichidagi raqamlarning OXIRGI
    #    4 tasini olamiz -> 1234. Real o'nlik teglar bu yo'lga TUSHMAYDI.
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 4:
        return digits[-4:]
    return digits or stripped
