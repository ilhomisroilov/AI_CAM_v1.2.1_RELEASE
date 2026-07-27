#!/usr/bin/env python3
"""
inspect_camera_payload.py — kamera payload faylini OFFLINE tahlil qiladi
=========================================================================
Diagnostika uchun (SOURCE kodni buzmaydi). Kodni ishga tushirmasdan, saqlangan
xom BLOB payload faylini decode_payload() bilan tekshiradi:
  - payload uzunligi
  - topilgan image magic offsetlar (BMP / JPEG / PNG)
  - aniqlangan format va source offset
  - dekodlangan tasvir shape
  - decode bo'lmasa: payload boshidan 128 bayt hex/ascii dump

Payload faylni qanday olish mumkin:
  * benchmark_camera.py --dump N   (har frame'ni .bin sifatida saqlaydi), yoki
  * pipeline log'idagi hexdump'dan, yoki
  * o'zingiz socketdan read_stream_frame() natijasini faylga yozib.

Foydalanish:
  python tools/inspect_camera_payload.py path/to/payload.bin
  python tools/inspect_camera_payload.py payload.bin --save out.png
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.camera.camera_client import decode_payload   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Kamera payload OFFLINE decode tahlili")
    ap.add_argument("payload", help="xom BLOB payload fayli (.bin)")
    ap.add_argument("--save", help="dekodlangan tasvirni shu yo'lga saqlash (.png)")
    ap.add_argument("--wh", help="RAW grayscale fallback uchun WxH (mas. 800x440)")
    args = ap.parse_args()

    hint_wh = None
    if args.wh:
        try:
            w, h = args.wh.lower().split("x")
            hint_wh = (int(w), int(h))
        except Exception:
            print(f"XATO: --wh formati WxH bo'lsin (mas. 800x440), berildi: {args.wh}")
            return 2

    if not os.path.exists(args.payload):
        print(f"XATO: fayl topilmadi: {args.payload}")
        return 2

    with open(args.payload, "rb") as f:
        payload = f.read()

    img, info = decode_payload(payload, hint_wh=hint_wh)

    print("=" * 60)
    print(f"Payload fayl : {args.payload}")
    print(f"Uzunlik      : {info['len']} bayt")
    print(f"Magic offset : BMP={info['bmp_off']}  JPEG={info['jpg_off']}  PNG={info['png_off']}")
    print(f"Format       : {info['format']}")
    print(f"Source offset: {info['source_off']}")
    print(f"Image shape  : {info['shape']}")
    print("=" * 60)

    if img is None:
        print("DECODE BO'LMADI. Payload boshi (128B):")
        print(info.get("hexdump") or "(bo'sh)")
        return 1

    print(f"DECODE OK ✓  format={info['format']} shape={info['shape']}")
    if args.save:
        import cv2
        cv2.imwrite(args.save, img)
        print(f"Saqlandi: {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
