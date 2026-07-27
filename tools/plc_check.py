"""
============================================================
plc_check.py  —  PLC ulanishini MUSTAQIL tekshirish (diagnostika)
============================================================
Bu skript BUTUN ilovani (OCR/kamera/RFID) chetlab o'tadi va FAQAT PLC ga
ulanishga urinadi. Maqsad: muammo PLC QURILMASIDA (tarmoq/RUN rejimi/MC
server) ekanini yoki dasturda ekanini ANIQ ajratish.

Ishlatish (serverda, AI_CAM papkasida):
    python tools/plc_check.py
    python tools/plc_check.py --ip 10.123.40.99 --port 5003 --signal D2222

Natijalar:
  * "ULANDI" + qiymat  -> PLC SOG'LOM. Muammo ilovada (leftover jarayon?).
  * "[Errno 111] Connection refused" -> PLC QURILMASI ulanishni rad etyapti:
        - PLC RUN rejimida emas (STOP/PROG),
        - MC-protokol serveri o'chirilgan yoki port noto'g'ri,
        - eski TCP ulanish osilib qolgan (PLC ni power-cycle qiling),
        - GX Works yoki boshqa mijoz ulanishni band qilgan,
        - tarmoq/kabel.
  * "timed out" / "No route to host" -> tarmoq muammosi (ping qiling).

DIQQAT: bu skript PLC ga HECH NARSA YOZMAYDI — faqat bitta registerni o'qiydi.
"""
from __future__ import annotations

import argparse
import socket
import sys


def tcp_probe(ip: str, port: int, timeout: float = 3.0) -> str:
    """Avval sof TCP darajada portni tekshiradi (pymcprotocol'siz)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        s.close()
        return "TCP port OCHIQ (qurilma port'da tinglayapti)"
    except Exception as exc:
        return f"TCP port YOPIQ/RAD: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Standalone PLC connection check")
    ap.add_argument("--ip", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--plctype", default=None)
    ap.add_argument("--signal", default=None)
    args = ap.parse_args()

    # config/settings.yaml dan standart qiymatlarni olamiz (agar berilmagan bo'lsa)
    ip, port, plctype, signal = args.ip, args.port, args.plctype, args.signal
    try:
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from backend.config import PLC
        ip = ip or PLC.ip
        port = port or PLC.port
        plctype = plctype or PLC.plc_type
        signal = signal or PLC.signal_address
    except Exception as exc:
        print(f"[eslatma] config o'qilmadi ({exc}); argumentlarni qo'lda bering.")
        ip = ip or "10.123.40.99"
        port = port or 5003
        plctype = plctype or "Q"
        signal = signal or "D2222"

    print(f"PLC tekshiruvi -> {ip}:{port}  plctype={plctype}  signal={signal}")
    print("1) TCP port:", tcp_probe(ip, port))

    try:
        import pymcprotocol
    except Exception as exc:
        print(f"2) pymcprotocol import qilinmadi: {exc}")
        print("   O'rnating:  pip install pymcprotocol")
        return 2

    print("2) MC protokol ulanishi...")
    mc = pymcprotocol.Type3E(plctype=plctype)
    try:
        mc.connect(ip, port)
    except Exception as exc:
        print(f"   ULANMADI: {exc}")
        print("   -> Bu PLC QURILMASI/tarmoq muammosi (ilova kodi emas).")
        return 1

    try:
        import re
        word = re.match(r"^\s*([A-Za-z]+)", signal or "").group(1).upper() in (
            "ZR", "SD", "SW", "SN", "TN", "CN", "D", "W", "R", "Z")
        if word:
            vals = mc.batchread_wordunits(headdevice=signal, readsize=1)
        else:
            vals = mc.batchread_bitunits(headdevice=signal, readsize=1)
        print(f"   ULANDI ✓  {signal} = {vals}")
        print("   -> PLC SOG'LOM. Agar ilova baribir ulanmasa, eski 'run.py' jarayoni")
        print("      hali ishlab, ulanishni band qilgan bo'lishi mumkin:")
        print("        ps aux | grep run.py     (eski PID ni kill qiling)")
        return 0
    except Exception as exc:
        print(f"   ULANDI, lekin o'qish xatosi: {exc}")
        print(f"   -> signal manzili ('{signal}') yoki plctype ('{plctype}') noto'g'ri bo'lishi mumkin.")
        return 1
    finally:
        try:
            mc.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
