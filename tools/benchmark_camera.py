#!/usr/bin/env python3
"""
benchmark_camera.py — HAQIQIY kamera read/decode benchmark (diagnostika)
=========================================================================
SOURCE pipeline'ni buzmaydi — faqat Lector652Client + decode_payload ni
to'g'ridan-to'g'ri ishlatib o'lchaydi:
  - read FPS        (socketdan payload o'qish tezligi)
  - decode FPS      (payload -> tasvir tezligi)
  - decode fail count
  - image shape(lar)
  - aniqlangan payload format(lar)

Foydalanish:
  python tools/benchmark_camera.py --ip 10.123.86.42 --frames 100
  python tools/benchmark_camera.py --ip 10.123.86.42 --frames 100 --dump 3
      (--dump N : birinchi N payloadni payload_000.bin ... sifatida saqlaydi —
       keyin inspect_camera_payload.py bilan tahlil qilish uchun)

Eslatma: kamera FAQAT bitta ulanish qabul qiladi — server (backend) ishlab
turgan bo'lsa, avval uni to'xtating, aks holda benchmark ulana olmaydi.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.camera.camera_client import Lector652Client, decode_payload  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Kamera read/decode benchmark")
    ap.add_argument("--ip", required=True, help="kamera IP")
    ap.add_argument("--cola-port", type=int, default=2111)
    ap.add_argument("--blob-port", type=int, default=2113)
    ap.add_argument("--password", default="")
    ap.add_argument("--frames", type=int, default=100, help="o'lchanadigan frame soni")
    ap.add_argument("--dump", type=int, default=0, help="birinchi N payloadni .bin ga saqlash")
    args = ap.parse_args()

    client = Lector652Client(ip=args.ip, control_port=args.cola_port,
                             blob_port=args.blob_port, password=args.password,
                             on_log=lambda m: None)   # log o'chiq (toza o'lchov)
    print(f"Ulanmoqda -> {args.ip} (CoLa {args.cola_port}, BLOB {args.blob_port}) ...")
    client.connect()
    client.start_stream()
    print(f"Stream boshlandi. {args.frames} frame o'lchanmoqda...\n")

    read_ms = []
    decode_ms = []
    fails = 0
    shapes = Counter()
    formats = Counter()
    t_start = time.monotonic()

    try:
        for i in range(args.frames):
            t0 = time.monotonic()
            payload = client.read_stream_frame()
            t1 = time.monotonic()
            img, info = decode_payload(payload,
                                      hint_wh=(client.img_width, client.img_height))
            t2 = time.monotonic()

            read_ms.append((t1 - t0) * 1000.0)
            decode_ms.append((t2 - t1) * 1000.0)
            if i < args.dump:
                fn = f"payload_{i:03d}.bin"
                with open(fn, "wb") as f:
                    f.write(payload)
            if img is None:
                fails += 1
                formats["FAIL"] += 1
            else:
                shapes[tuple(img.shape)] += 1
                formats[info.get("format") or "?"] += 1
    finally:
        try:
            client.stop_stream()
            client.disconnect()
        except Exception:
            pass

    elapsed = time.monotonic() - t_start
    n = max(1, len(read_ms))
    avg_read = sum(read_ms) / n
    avg_dec = sum(decode_ms) / n
    read_fps = (len(read_ms) / elapsed) if elapsed > 0 else 0.0
    decode_ok = len(read_ms) - fails
    decode_fps = (decode_ok / elapsed) if elapsed > 0 else 0.0

    print("=" * 60)
    print("BENCHMARK NATIJASI")
    print("=" * 60)
    print(f"  Frames o'qildi   : {len(read_ms)}")
    print(f"  Davomiylik       : {elapsed:.2f}s")
    print(f"  Read FPS         : {read_fps:.1f}  (avg {avg_read:.1f} ms/frame)")
    print(f"  Decode FPS       : {decode_fps:.1f}  (avg {avg_dec:.1f} ms/frame)")
    print(f"  Decode FAIL      : {fails} / {len(read_ms)}")
    print(f"  Formatlar        : {dict(formats)}")
    print(f"  Shape(lar)       : {dict(shapes)}")
    print("=" * 60)
    if fails == 0 and decode_ok > 0:
        print("✓ Acceptance: decode fail = 0")
        return 0
    print("✗ Decode fail > 0 — inspect_camera_payload.py bilan payloadni tekshiring "
          "(--dump bilan saqlangan .bin).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
