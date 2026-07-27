# Merge Review — production-hardening "triggering" shared-fayl o'zgarishlari

Sizning `production-hardening` liniyangiz (`d1d6d5c4`, 2026-06-26) v1.1.2 baseline'ga (`628a804`) nisbatan UMUMIY (shared) fayllarda quyidagilarni o'zgartirgan. Bu o'zgarishlar MVP-hardening rewrite'i bilan ZIDDIYATLI bo'lgani uchun integratsiyaga AVTOMATIK kiritilmadi. Quyida ularni ko'rib chiqib, kerakli qismlarni hardened kod ustiga qayta qo'llashingiz mumkin.

## O'zgargan shared source fayllar (stat)
```
 backend/ai/ocr_worker.py        | 157 ++++++++++++++++++++++++-------
 backend/ai/vin_postprocess.py   |  40 +++++---
 backend/camera/camera_client.py | 204 ++++++++++++++++++++++++++++++++++------
 backend/database/db.py          |  53 ++++++++---
 backend/plc/plc_base.py         |   7 ++
 backend/plc/plc_melsec.py       |  20 ++++
 backend/plc/plc_service.py      | 126 ++++++++++++++++++++-----
 backend/plc/plc_simulator.py    |  17 +++-
 config/settings.yaml            |  21 ++++-
 9 files changed, 524 insertions(+), 121 deletions(-)
```

## To'liq diff (source fayllar)
```diff
diff --git a/backend/ai/ocr_worker.py b/backend/ai/ocr_worker.py
index c42504a..618b0ef 100644
--- a/backend/ai/ocr_worker.py
+++ b/backend/ai/ocr_worker.py
@@ -44,8 +44,8 @@ from ..logger import log
 from . import vin_rules
 from .vin_postprocess import postprocess
 
-# on_result(validated_vin, score, crop_bgr, raw_vin, model)
-ResultCallback = Callable[[str, float, np.ndarray, str, object], None]
+# on_result(validated_vin, score, crop_bgr, raw_vin, model, session_id)
+ResultCallback = Callable[[str, float, np.ndarray, str, object, object], None]
 
 # Standart avtomobil VIN strukturasi: aniq 17 belgi, alfanumerik,
 # I, O, Q HARFLARI YO'Q (ISO 3779). Regex bilan qat'iy tekshiramiz.
@@ -132,8 +132,25 @@ class _PaddleEngine:
         if self._ocr is None:
             raise RuntimeError(f"PaddleOCR init muvaffaqiyatsiz: {last_err}")
         self._api_v3 = ("device" in used_kw or "use_textline_orientation" in used_kw)
-        self.gpu = gpu
-        log.info(f"PaddleOCR init OK: {sorted(used_kw.keys())} (gpu={gpu})")
+        # HAQIQIY device ni Paddle dan so'raymiz — app bergan `gpu` bayrog'i emas.
+        # (Log "gpu=True" desa-yu, Paddle CPU build bo'lsa, bu chalg'itar edi.)
+        real_dev = "gpu" if gpu else "cpu"
+        real_gpu = gpu
+        try:
+            import paddle
+            compiled_cuda = bool(paddle.device.is_compiled_with_cuda())
+            real_dev = str(paddle.device.get_device())     # "gpu:0" yoki "cpu"
+            real_gpu = compiled_cuda and real_dev.lower().startswith("gpu")
+            if gpu and not real_gpu:
+                log.warning("PaddleOCR: GPU so'raldi, lekin Paddle CPU build/qurilmada "
+                            f"ishlayapti (compiled_with_cuda={compiled_cuda}, device={real_dev}). "
+                            "GPU OCR uchun paddlepaddle-gpu o'rnating.")
+        except Exception as exc:
+            log.warning(f"PaddleOCR device tekshirib bo'lmadi: {exc}")
+        self.gpu = real_gpu                                 # HAQIQIY holat saqlanadi
+        self._device_str = real_dev
+        log.info(f"PaddleOCR init OK: {sorted(used_kw.keys())} "
+                 f"(requested_gpu={gpu}, real_device={real_dev}, gpu={real_gpu})")
 
     def _is_cuda_error(self, exc: Exception) -> bool:
         msg = str(exc).lower()
@@ -229,8 +246,11 @@ class _PaddleEngine:
 class OCRWorker:
     """EasyOCR ni fon threadida navbat orqali ishlatadi (aniqlik + nazorat)."""
 
-    def __init__(self, on_result: ResultCallback) -> None:
+    def __init__(self, on_result: ResultCallback, on_fail=None) -> None:
         self.on_result = on_result
+        # on_fail(session_id, raw_text) — yaroqli VIN chiqmaganda (yoki model=None rad
+        # bo'lganda) chaqiriladi. Pipeline plastinka qulfini qayta ochib retry qiladi.
+        self.on_fail = on_fail
         self._queue: "queue.Queue" = queue.Queue(maxsize=OCR.queue_maxsize)
         self._thread: Optional[threading.Thread] = None
         self._running = False
@@ -241,9 +261,10 @@ class OCRWorker:
         self._min_conf = OCR.min_confidence
         self._last_run = 0.0                     # throttle (monotonic)
 
-        # Anti-duplicate
+        # Anti-duplicate (P2 FIX: session-aware — faqat BIR sessiya ichida)
         self._last_vin: Optional[str] = None
         self._last_vin_ts: float = 0.0
+        self._last_vin_session: object = None
         self._dup_lock = threading.Lock()
 
         # CLAHE
@@ -273,20 +294,50 @@ class OCRWorker:
         return self._ensure_reader()
 
     def start(self) -> None:
-        if self._running:
-            return
+        """
+        P3 FIX: UZLUKSIZ worker. Sessiya boshida CHAQIRILADI, lekin worker threadни
+        QAYTA-QAYTA yaratmaydi — agar thread allaqachon tirik bo'lsa, faqat OCR ni
+        yoqadi (drain qilmaydi). Bu "stop-during-job -> qolgan sentinel keyingi
+        workerни o'ldiradi" poyga shartini butunlay yo'q qiladi (sentinel yo'q).
+        """
+        self._enabled = True
+        if self._thread is not None and self._thread.is_alive():
+            return                               # uzluksiz worker allaqachon ishlayapti
         self._running = True
         self._thread = threading.Thread(target=self._loop, name="ocr-worker", daemon=True)
         self._thread.start()
-        log.info("OCR worker ishga tushdi (PaddleOCR, fon navbat).")
+        log.info("OCR worker ishga tushdi (PaddleOCR, uzluksiz fon navbat).")
 
     def stop(self) -> None:
+        """
+        P3 FIX: SESSIYA chegarasi — OCR ni PAUZA qiladi va navbatni tozalaydi, lekin
+        worker threadни O'LDIRMAYDI. Sentinel ishlatilmaydi -> keyingi sessiyada
+        worker tirik qoladi va croplarni qayta ishlaydi. To'liq to'xtatish: shutdown().
+        """
+        self._enabled = False
+        self._drain_queue()                      # eski sessiya croplari tozalanadi
+        log.info("OCR worker pauza qilindi (sessiya chegarasi; thread tirik).")
+
+    def shutdown(self) -> None:
+        """Ilova to'xtaganda worker threadни butunlay to'xtatadi (graceful)."""
         self._running = False
+        self._enabled = False
+        self._drain_queue()
+        t = self._thread
+        # stop()/shutdown() ba'zan worker threadning O'ZIDAN chaqirilishi mumkin —
+        # o'zini join qilmaymiz.
+        if t is not None and t is not threading.current_thread():
+            t.join(timeout=2.0)
+        self._thread = None
+        log.info("OCR worker to'liq to'xtatildi (shutdown).")
+
+    def _drain_queue(self) -> None:
+        """Navbatdagi barcha kutilayotgan ishlarni tashlaydi (sessiyalar aralashmasin)."""
         try:
-            self._queue.put_nowait(None)         # sentinel
-        except queue.Full:
+            while True:
+                self._queue.get_nowait()
+        except queue.Empty:
             pass
-        log.info("OCR worker to'xtatildi.")
 
     # ===============================================================
     # Dinamik nazorat (monitoring & control)
@@ -321,18 +372,21 @@ class OCRWorker:
     # ===============================================================
     # Navbat (drop-oldest — FPS himoyasi)
     # ===============================================================
-    def submit(self, crop_bgr: np.ndarray, model: Optional[str] = None) -> None:
+    def submit(self, crop_bgr: np.ndarray, model: Optional[str] = None,
+               session_id: Optional[int] = None) -> None:
         """Bitta crop (orqaga moslik) — ko'p-kadrli ish sifatida o'raladi."""
-        self.submit_frames([crop_bgr], model)
+        self.submit_frames([crop_bgr], model, session_id)
 
-    def submit_frames(self, crops: list, model: Optional[str] = None) -> None:
+    def submit_frames(self, crops: list, model: Optional[str] = None,
+                      session_id: Optional[int] = None) -> None:
         """
         Bitta plastinka hodisasi uchun ENG YAXSHI croplar ro'yxati (multi-frame
-        fusion) + plastinka modeli (QY/BL7M). OCR ovoz berib yagona VIN chiqaradi.
+        fusion) + plastinka modeli + SESSIYA ID. Callback session_id ni qaytaradi —
+        pipeline eski sessiya cropi yangi sessiyaga yozilishining oldini oladi.
         """
         if not self._enabled or not crops:
             return
-        job = (crops, model)
+        job = (crops, model, session_id)
         try:
             self._queue.put_nowait(job)
         except queue.Full:
@@ -383,19 +437,26 @@ class OCRWorker:
             return
         while self._running:
             try:
-                job = self._queue.get(timeout=1.0)
+                job = self._queue.get(timeout=0.5)
             except queue.Empty:
                 continue
-            if job is None:                      # stop sentinel
-                break
-            # Throttle: ketma-ket OCR orasidagi minimal vaqt
+            if job is None:                      # eskirgan sentinel (himoya) — e'tiborsiz
+                continue
+            # P8 FIX: throttle endi job NI TASHLAMAYDI. Ketma-ket OCR orasidagi minimal
+            # vaqtni kutib (sleep), so'ng ishlaydi — hech qaysi crop yo'qolmaydi.
             if OCR.min_interval_sec > 0:
                 dt = time.monotonic() - self._last_run
-                if dt < OCR.min_interval_sec:
-                    continue                     # bu ishni o'tkazib yuboramiz (lag himoyasi)
+                wait = OCR.min_interval_sec - dt
+                if wait > 0:
+                    # uzun kutishni bo'laklarga bo'lamiz (stop ga sezgir)
+                    end = time.monotonic() + wait
+                    while self._running and time.monotonic() < end:
+                        time.sleep(min(0.1, end - time.monotonic()))
+                    if not self._running:
+                        break
             self._last_run = time.monotonic()
-            crops, model = job
-            self._process_job(crops, model)
+            crops, model, session_id = job
+            self._process_job(crops, model, session_id)
 
     # ===============================================================
     # Preprocess — aniqlik uchun (kattalashtirish + CLAHE + sharpen)
@@ -505,7 +566,17 @@ class OCRWorker:
         score, raw, crop = max(entries, key=lambda e: e[0])
         return winner, score, raw, crop
 
-    def _process_job(self, crops: list, model=None) -> None:
+    def _emit_fail(self, session_id, raw: str) -> None:
+        """Yaroqli VIN chiqmadi -> pipeline ga xabar (plastinka qulfi qayta ochiladi)."""
+        cb = self.on_fail
+        if cb is None:
+            return
+        try:
+            cb(session_id, raw)
+        except Exception as exc:
+            log.error(f"on_fail callback xatosi: {exc}")
+
+    def _process_job(self, crops: list, model=None, session_id=None) -> None:
         """
         Bitta plastinka hodisasi: ENG YAXSHI croplarda OCR + retry + VIN-aware
         post-processing + fusion. Xom OCR HAR DOIM saqlanadi (audit).
@@ -553,6 +624,7 @@ class OCRWorker:
                 self._rejected += 1
             log.info(f"OCR: {len(crops)} kadrdan yaroqli VIN topilmadi "
                      f"(xom='{last_raw}', {attempts} urinish, {dt_ms:.0f} ms).")
+            self._emit_fail(session_id, last_raw)      # pipeline qulfni qayta ochsin (retry)
             return
 
         # --- Fusion: validated VIN bo'yicha ko'pchilik ovozi ---
@@ -562,9 +634,23 @@ class OCRWorker:
         with self._stats_lock:
             self._last_conf = score
 
-        # --- Anti-duplicate ---
-        if self._is_duplicate(vin):
-            log.info(f"DUPLICATE: '{vin}' yaqinda o'qilgan — e'tiborsiz qoldirildi.")
+        # --- Production: model (QY/BL7M) aniqlanmasa VIN RAD ETILADI ---
+        # (HSTFC... kabi qoidaga mos kelmaydigan VINlar generic fallback orqali
+        #  o'tib ketmasin. require_known_model=False bo'lsa eski xatti-harakat.)
+        if VIN.require_known_model and not final_model:
+            with self._stats_lock:
+                self._rejected += 1
+            log.warning(f"OCR: VIN '{vin}' model aniqlanmadi (QY/BL7M emas) — RAD ETILDI "
+                        f"(xom='{raw}'). require_known_model=True.")
+            self._emit_fail(session_id, raw)
+            return
+
+        # --- Anti-duplicate (P2: faqat BIR sessiya ichidagi takror submit) ---
+        # YANGI sessiya (boshqa session_id) bir xil VIN bilan kelsa — bu YANGI
+        # avtomobil hodisasi; bloklanmasin va on_result CHAQIRILSIN (aks holda
+        # sessiya 90s TIMEOUT ga osilib, soxta NO_READ yozilardi).
+        if self._is_duplicate(vin, session_id):
+            log.info(f"DUPLICATE: '{vin}' shu sessiyada yaqinda o'qilgan — e'tiborsiz.")
             return
 
         with self._stats_lock:
@@ -573,16 +659,23 @@ class OCRWorker:
         log.info(f"VIN aniqlandi: {vin} [model={final_model}, score={score:.2f}, xom='{raw}'{changed}, "
                  f"{len(candidates)}/{len(crops)} kadr rozi, {attempts} urinish, {dt_ms:.0f} ms]")
         try:
-            self.on_result(vin, score, best_crop, raw, final_model)
+            self.on_result(vin, score, best_crop, raw, final_model, session_id)
         except Exception as exc:
             log.error(f"on_result callback xatosi: {exc}")
 
-    def _is_duplicate(self, vin: str) -> bool:
+    def _is_duplicate(self, vin: str, session_id=None) -> bool:
+        """
+        P2 FIX: dublikat FAQAT bir xil VIN + bir xil sessiya (yoki ikkalasi ham
+        qo'lda rejim=None) bo'lganda. Boshqa session_id -> yangi avtomobil, hech
+        qachon dublikat emas (bloklanmaydi).
+        """
         now = time.time()
         with self._dup_lock:
-            if (self._last_vin == vin
+            same_session = (session_id == self._last_vin_session)
+            if (self._last_vin == vin and same_session
                     and (now - self._last_vin_ts) < OCR.duplicate_window_sec):
                 return True
             self._last_vin = vin
             self._last_vin_ts = now
+            self._last_vin_session = session_id
             return False
diff --git a/backend/ai/vin_postprocess.py b/backend/ai/vin_postprocess.py
index 96244b6..628b89f 100644
--- a/backend/ai/vin_postprocess.py
+++ b/backend/ai/vin_postprocess.py
@@ -26,23 +26,35 @@ from typing import Dict, List, Optional, Tuple
 from . import vin_rules
 
 # --- OCR vizual chalkashliklar (ikki tomonlama) ---
-# 1↔I, 1↔T, S↔5, O↔0, B↔8, G↔6, A↔4, E↔F
+# P14 FIX: etched/dot-peen metall VIN uchun keng tarqalgan misread juftlari
+# kengaytirildi (Z↔2, D↔0, 5↔6, 2↔7, 8↔0, B↔3, 7↔1...). "Never invent" siyosati
+# saqlanadi: strukturaviy belgi FAQAT OCR belgisining chalkashlik to'plamida
+# bo'lsa tanlanadi. (I/O/Q VIN da yo'q — lekin O/0, I/1 OCR darajasida bo'ladi.)
 CONFUSIONS: Dict[str, Tuple[str, ...]] = {
-    "1": ("I", "T"),
-    "I": ("1",),
-    "T": ("1",),
-    "S": ("5",),
-    "5": ("S",),
-    "O": ("0",),
-    "0": ("O",),
-    "B": ("8",),
-    "8": ("B",),
-    "G": ("6",),
-    "6": ("G",),
-    "A": ("4",),
+    "0": ("O", "D", "Q", "8"),
+    "1": ("I", "T", "7", "L"),
+    "2": ("Z", "7"),
+    "3": ("8", "B"),
     "4": ("A",),
+    "5": ("S", "6"),
+    "6": ("G", "5", "8"),
+    "7": ("1", "2", "T"),
+    "8": ("B", "0", "3", "6"),
+    "9": ("0",),
+    "A": ("4",),
+    "B": ("8", "3", "R"),
+    "D": ("0", "O"),
     "E": ("F",),
-    "F": ("E",),
+    "F": ("E", "P"),
+    "G": ("6", "C"),
+    "I": ("1",),
+    "L": ("1",),
+    "O": ("0", "D", "Q"),
+    "Q": ("0", "O"),
+    "R": ("B",),
+    "S": ("5",),
+    "T": ("1", "7"),
+    "Z": ("2",),
 }
 
 
diff --git a/backend/camera/camera_client.py b/backend/camera/camera_client.py
index 81b90c4..cf09e96 100644
--- a/backend/camera/camera_client.py
+++ b/backend/camera/camera_client.py
@@ -181,7 +181,21 @@ def _read_blob_frame(sock: socket.socket) -> bytes:
     return payload
 
 
-# -- Payload -> tasvir (AYLANTIRISH YO'Q) ------------------------------------
+# -- Payload -> tasvir (ROBUST multi-format dekod, AYLANTIRISH YO'Q) ----------
+#
+# Kamera "BM" (BMP) uzatishi kerak, lekin payload formati har doim
+# "19 bayt sub-header + BMP" bo'lishi SHART EMAS. Ba'zan oldida XML/report
+# bo'ladi yoki sub-header uzunligi farq qiladi. Shuning uchun payload ichidan
+# HAQIQIY image magic'larni topamiz va eng ishonchli candidate'ni dekod qilamiz.
+
+# Image magic'lar (BMP ustuvor — kamera BMP uzatadi)
+_MAGIC_BMP  = b"BM"
+_MAGIC_JPEG = b"\xff\xd8\xff"
+_MAGIC_PNG  = b"\x89PNG\r\n\x1a\n"
+
+# Log spamini oldini olish uchun — format/shape o'zgarganda bir marta INFO
+_last_decode_sig: Optional[tuple] = None
+
 
 def _decode_bmp_8bpp(bmp: bytes) -> Optional[np.ndarray]:
     """Zaxira: 8bpp grayscale BMP ni to'g'ridan-to'g'ri numpy ga (imdecode ishlamasa)."""
@@ -210,41 +224,157 @@ def _decode_bmp_8bpp(bmp: bytes) -> Optional[np.ndarray]:
         return None
 
 
-def decode_bmp(payload: bytes) -> Optional[np.ndarray]:
+def _find_bmp_offset(payload: bytes) -> int:
     """
-    Live BLOB payload -> grayscale numpy (XOM, AYLANTIRISH YO'Q).
-
-    Asosiy yo'l (talab bo'yicha):
-        1. Birinchi 19 baytlik SICK sub-header tashlanadi: payload[19:]
-        2. cv2.imdecode(np.frombuffer(..., uint8), IMREAD_GRAYSCALE)
-
-    Zaxira (sub-header uzunligi boshqacha bo'lsa sindirmaslik uchun):
-        3. payload ichidan 'BM' magic topib, o'sha joydan imdecode
-        4. 8bpp BMP to'g'ridan-to'g'ri numpy
+    'BM' magic'ni topadi, lekin FAQAT header'i ishonchli bo'lganini (tasodifiy
+    'BM' ketma-ketligi emas). BMP header: 'BM' + fileSize(4 LE) + reserved(4) +
+    pixelDataOffset(4 LE). Topilmasa -1.
+    """
+    start = 0
+    n = len(payload)
+    while True:
+        i = payload.find(_MAGIC_BMP, start)
+        if i < 0 or i + 54 > n:
+            return -1
+        try:
+            file_size = int.from_bytes(payload[i + 2:i + 6], "little")
+            px_off    = int.from_bytes(payload[i + 10:i + 14], "little")
+            dib_size  = int.from_bytes(payload[i + 14:i + 18], "little")
+            avail     = n - i
+            # Ishonchlilik: pixel offset header ichida, DIB header standart (12..124),
+            # fayl o'lchami mavjud baytlardan oshmasligi (kichik xatolikka yo'l qo'yamiz).
+            if 26 <= px_off <= avail and 12 <= dib_size <= 124 and 0 < file_size <= avail + 64:
+                return i
+        except Exception:
+            pass
+        start = i + 2
+    # erishilmaydi
+
+
+def _hexdump(data: bytes, length: int = 128) -> str:
+    """Diagnostika uchun payload boshidan hex + ascii dump (decode fail bo'lganda)."""
+    chunk = data[:length]
+    lines = []
+    for off in range(0, len(chunk), 16):
+        row = chunk[off:off + 16]
+        hexs = " ".join(f"{b:02x}" for b in row)
+        asci = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
+        lines.append(f"  {off:04x}  {hexs:<47}  {asci}")
+    return "\n".join(lines)
+
+
+def _decode_raw_gray(payload: bytes, hint_wh) -> Optional[np.ndarray]:
+    """
+    SICK-aware RAW grayscale fallback: container (BMP/JPEG/PNG) topilmasa, kamera
+    e'lon qilgan W×H (mDIGetEffImgSize) bo'yicha xom 8bpp baytlarni reshape qiladi.
 
-    Hech qaysi bosqichda 180° aylantirish QO'LLANILMAYDI.
+    SICK Lector odatda: [header/metadata] + [xom rasm]. Shuning uchun payload
+    OXIRIDAN W*H bayt olinadi (eng ishonchli). Faqat payload kerakli o'lcham(lar)dan
+    katta bo'lsa qo'llanadi -> kichik (JPEG bo'lishi mumkin) payloadlar reshape
+    qilinmaydi (noto'g'ri tasvir oldini olish).
     """
-    if not payload or len(payload) <= SICK_SUBHEADER_LEN:
+    if not hint_wh:
+        return None
+    w, h = int(hint_wh[0] or 0), int(hint_wh[1] or 0)
+    if w <= 0 or h <= 0:
+        return None
+    need = w * h
+    n = len(payload)
+    if n < need:
+        return None                          # raw 8bpp uchun juda kichik (ehtimol JPEG)
+    # Oxiridan W*H bayt (image header'dan keyin keladi)
+    try:
+        tail = payload[n - need:]
+        return np.frombuffer(tail, dtype=np.uint8).reshape(h, w)
+    except Exception:
         return None
 
-    # 1+2) ANIQ talab: 19 baytni o'tkazib, grayscale imdecode
-    body = payload[SICK_SUBHEADER_LEN:]
-    img = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
-    if img is not None:
-        return img                                  # AYLANTIRISH YO'Q
-
-    # 3) Zaxira: 'BM' ni topib o'sha joydan imdecode (sub-header != 19 bo'lsa)
-    idx = payload.find(b"BM")
-    if idx >= 0:
-        bmp = payload[idx:]
-        img = cv2.imdecode(np.frombuffer(bmp, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
-        if img is not None:
-            return img
-        # 4) Zaxira: 8bpp raw BMP parse
-        return _decode_bmp_8bpp(bmp)
 
-    log.warning("decode_bmp: tasvir dekodlanmadi (payload len=%d).", len(payload))
-    return None
+def decode_payload(payload: bytes, hint_wh=None):
+    """
+    Live BLOB payload -> (grayscale numpy | None, info dict).  XOM, AYLANTIRISH YO'Q.
+
+    Robust strategiya (BMP ustuvor — kamera BMP uzatadi):
+      1. Payload ichidan image magic offsetlarini topadi: BMP('BM'),
+         JPEG(FF D8 FF), PNG(89 50 4E 47...). XML/report prefiks bo'lsa ham
+         o'tib, keyingi image section'ni topadi.
+      2. Candidate'larni TARTIB bilan sinaydi: BMP -> JPEG -> PNG, plus
+         orqaga moslik uchun payload[19:] va payload[0:].
+      3. BMP uchun cv2.imdecode ishlamasa 8bpp raw parser fallback.
+      4. Container topilmasa — SICK W×H (hint_wh) bo'yicha RAW grayscale reshape.
+      5. Hech narsa decode bo'lmasa info ga 128-bayt hexdump qo'shiladi.
+
+    hint_wh: (width, height) — kamera mDIGetEffImgSize dan (RAW fallback uchun).
+    info: {len, bmp_off, jpg_off, png_off, format, source_off, shape, hexdump}
+    """
+    info = {"len": len(payload) if payload else 0, "bmp_off": -1, "jpg_off": -1,
+            "png_off": -1, "format": None, "source_off": -1, "shape": None,
+            "hexdump": None}
+    if not payload or len(payload) < 4:
+        info["hexdump"] = _hexdump(payload or b"")
+        return None, info
+
+    bmp_off = _find_bmp_offset(payload)
+    jpg_off = payload.find(_MAGIC_JPEG)
+    png_off = payload.find(_MAGIC_PNG)
+    info.update(bmp_off=bmp_off, jpg_off=jpg_off, png_off=png_off)
+
+    # Candidate ro'yxati: (format, offset). BMP ustuvor.
+    candidates = []
+    if bmp_off >= 0:
+        candidates.append(("BMP", bmp_off))
+    # Orqaga moslik: ko'p kamera "19B sub-header + BMP" yuboradi
+    if len(payload) > SICK_SUBHEADER_LEN:
+        candidates.append(("BMP?", SICK_SUBHEADER_LEN))
+    candidates.append(("RAW", 0))
+    if jpg_off >= 0:
+        candidates.append(("JPEG", jpg_off))
+    if png_off >= 0:
+        candidates.append(("PNG", png_off))
+
+    for fmt, off in candidates:
+        if off < 0 or off >= len(payload):
+            continue
+        body = payload[off:]
+        img = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
+        if img is None and fmt in ("BMP", "BMP?", "RAW"):
+            img = _decode_bmp_8bpp(body)         # 8bpp raw fallback
+        if img is not None and getattr(img, "size", 0) > 0:
+            real_fmt = ("BMP" if fmt in ("BMP", "BMP?") and body[:2] == _MAGIC_BMP else
+                        "JPEG" if body[:3] == _MAGIC_JPEG else
+                        "PNG" if body[:8] == _MAGIC_PNG else
+                        ("BMP" if body[:2] == _MAGIC_BMP else "UNKNOWN"))
+            info.update(format=real_fmt, source_off=off, shape=tuple(img.shape))
+            _maybe_log_decode(info)
+            return img, info
+
+    # 4) SICK RAW grayscale fallback (container topilmadi) — kamera W×H bo'yicha
+    raw = _decode_raw_gray(payload, hint_wh)
+    if raw is not None and raw.size > 0:
+        info.update(format="RAW8", source_off=len(payload) - raw.size, shape=tuple(raw.shape))
+        _maybe_log_decode(info)
+        return raw, info
+
+    # Hech narsa decode bo'lmadi — diagnostika
+    info["hexdump"] = _hexdump(payload)
+    return None, info
+
+
+def _maybe_log_decode(info: dict) -> None:
+    """Format/shape o'zgarganda BIR MARTA INFO (har frame'da log spam bo'lmasin)."""
+    global _last_decode_sig
+    sig = (info.get("format"), info.get("shape"), info.get("source_off"))
+    if sig != _last_decode_sig:
+        _last_decode_sig = sig
+        log.info("Kamera dekod: format=%s shape=%s offset=%d (payload=%d B)",
+                 info.get("format"), info.get("shape"), info.get("source_off"),
+                 info.get("len"))
+
+
+def decode_bmp(payload: bytes, hint_wh=None) -> Optional[np.ndarray]:
+    """Orqaga moslik: faqat tasvirni qaytaradi (decode_payload ustidan wrapper)."""
+    img, _info = decode_payload(payload, hint_wh=hint_wh)
+    return img
 
 
 # -- Asosiy klient ------------------------------------------------------------
@@ -274,6 +404,10 @@ class Lector652Client:
         self._blob: socket.socket | None = None
         self._live_active  = False
         self._last_recv_ms = 0.0
+        # Kamera e'lon qilgan effektiv rasm o'lchami (mDIGetEffImgSize) — RAW
+        # grayscale fallback uchun (container topilmasa shu W×H bilan reshape).
+        self.img_width:  int = 0
+        self.img_height: int = 0
 
     # -- Ulanish --------------------------------------------------------------
 
@@ -326,6 +460,16 @@ class Lector652Client:
         self._cola("sRN VITmeStatDisp")
         resp = self._cola("sMN mDIGetEffImgSize")
         self._info("Effektiv rasm o'lchami: {}".format(resp))
+        # Javobdan W H ni ajratamiz: "sAN mDIGetEffImgSize 800 440" -> (800, 440)
+        try:
+            parts = resp.split()
+            ints = [int(p) for p in parts if p.isdigit()]
+            if len(ints) >= 2:
+                self.img_width, self.img_height = ints[-2], ints[-1]
+                self._info("Effektiv geometriya: {}x{} (RAW grayscale fallback uchun).".format(
+                    self.img_width, self.img_height))
+        except Exception as exc:
+            self._info("[!] EffImgSize parse: {}".format(exc))
 
         # BLOB socket (dinamik IP) — mustahkam sozlamalar bilan
         self._info("Blob -> {}:{}".format(self.ip, self.blob_port))
diff --git a/backend/database/db.py b/backend/database/db.py
index 8a12496..3c74a77 100644
--- a/backend/database/db.py
+++ b/backend/database/db.py
@@ -34,7 +34,7 @@ _lock = threading.Lock()
 # Jadval ustunlari (tartibda) — SELECT/eksport uchun
 # rfid_epc = ajratilgan RFID raqami (1000..9999); rfid_raw = to'liq xom EPC (audit)
 COLUMNS = ["id", "timestamp", "detected_vin", "raw_vin", "model", "rfid_epc",
-           "rfid_raw", "confidence", "status", "image_path"]
+           "rfid_raw", "confidence", "status", "image_path", "session_id"]
 
 
 def _connect() -> sqlite3.Connection:
@@ -66,6 +66,9 @@ def _migrate(conn) -> None:
     if "rfid_raw" not in cols:
         conn.execute("ALTER TABLE vin_records ADD COLUMN rfid_raw TEXT")
         log.info("DB migratsiya: 'rfid_raw' ustuni qo'shildi.")
+    if "session_id" not in cols:
+        conn.execute("ALTER TABLE vin_records ADD COLUMN session_id INTEGER")
+        log.info("DB migratsiya: 'session_id' ustuni qo'shildi.")
 
 
 def init_db() -> None:
@@ -83,12 +86,17 @@ def init_db() -> None:
                 rfid_raw      TEXT,
                 confidence    REAL    NOT NULL,
                 status        TEXT,
-                image_path    TEXT
+                image_path    TEXT,
+                session_id    INTEGER
             )
             """
         )
         _migrate(conn)   # mavjud (eski) bazalar uchun
         conn.execute("CREATE INDEX IF NOT EXISTS idx_vin_ts ON vin_records(timestamp)")
+        # P4 FIX: bir PLC sessiyasi = bir qator (idempotentlik). session_id NULL
+        # bo'lmagan qatorlar uchun UNIQUE (qo'lda/legacy yozuvlar NULL -> cheklanmaydi).
+        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_vin_session "
+                     "ON vin_records(session_id) WHERE session_id IS NOT NULL")
     log.info(f"Ma'lumotlar bazasi tayyor: {DB_PATH}")
 
 
@@ -98,26 +106,41 @@ def insert_record(timestamp: str, vin: str, confidence: float,
                   model: Optional[str] = None,
                   status: str = "OK",
                   rfid_epc: Optional[str] = None,
-                  rfid_raw: Optional[str] = None) -> int:
+                  rfid_raw: Optional[str] = None,
+                  session_id: Optional[int] = None) -> int:
     """
     Yangi VIN+RFID yozuvini qo'shadi (BIR PLC trigger = BIR qator).
-      vin       -> validated (saralangan) VIN
-      raw_vin   -> xom OCR natijasi (audit)
-      model     -> QY | BL7M (VIN prefiksidan)
-      rfid_epc  -> ajratilgan RFID raqami (1000..9999)
-      rfid_raw  -> to'liq xom EPC (audit/traceability)
-      status    -> ishlov berish holati (OK / ...)
+      vin        -> validated (saralangan) VIN
+      raw_vin    -> xom OCR natijasi (audit)
+      model      -> QY | BL7M (VIN prefiksidan)
+      rfid_epc   -> ajratilgan RFID raqami (1000..9999)
+      rfid_raw   -> to'liq xom EPC (audit/traceability)
+      status     -> ishlov berish holati (OK / ...)
+      session_id -> PLC sessiya ID (idempotentlik: bir sessiya = bir qator).
+                    Bir xil session_id qayta yozilsa, mavjud qator ID si qaytadi
+                    (P4: kech natija qo'sh yozuv yaratmaydi).
     """
     with _lock, _connect() as conn:
         cur = conn.execute(
-            "INSERT INTO vin_records "
+            "INSERT OR IGNORE INTO vin_records "
             "(timestamp, detected_vin, raw_vin, model, rfid_epc, rfid_raw, "
-            " confidence, status, image_path) "
-            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
+            " confidence, status, image_path, session_id) "
+            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (timestamp, vin, raw_vin, model, rfid_epc, rfid_raw,
-             confidence, status, image_path),
+             confidence, status, image_path, session_id),
         )
-        return int(cur.lastrowid)
+        if cur.rowcount and cur.lastrowid:
+            return int(cur.lastrowid)
+        # IGNORE bo'ldi (bir xil session_id allaqachon bor) -> mavjud qator ID si
+        if session_id is not None:
+            row = conn.execute(
+                "SELECT id FROM vin_records WHERE session_id = ?", (session_id,)
+            ).fetchone()
+            if row is not None:
+                log.warning(f"DB: session_id={session_id} allaqachon yozilgan "
+                            f"(qator #{row[0]}) — qo'sh yozuv oldini olindi (idempotent).")
+                return int(row[0])
+        return int(cur.lastrowid or 0)
 
 
 def get_records(limit: int = 500, order: str = "DESC",
@@ -167,7 +190,7 @@ def get_records(limit: int = 500, order: str = "DESC",
     with _lock, _connect() as conn:
         rows = conn.execute(
             f"SELECT id, timestamp, detected_vin, raw_vin, model, rfid_epc, rfid_raw, "
-            f"confidence, status, image_path "
+            f"confidence, status, image_path, session_id "
             f"FROM vin_records{where_sql} ORDER BY {sort_col} {direction} LIMIT ?",
             tuple(params),
         ).fetchall()
diff --git a/backend/plc/plc_base.py b/backend/plc/plc_base.py
index 325b080..48927b7 100644
--- a/backend/plc/plc_base.py
+++ b/backend/plc/plc_base.py
@@ -30,6 +30,13 @@ class PLCInterface:
         """
         raise NotImplementedError
 
+    def write_signal(self, address: str, value: int) -> bool:
+        """
+        Handshake/DONE bitini yoki registerni yozadi (P6 fix). Qo'llab-quvvatlanmasa
+        False. Simulyatorда no-op (True).
+        """
+        return False
+
     def close(self) -> None:
         """Resurslarni bo'shatadi."""
         pass
diff --git a/backend/plc/plc_melsec.py b/backend/plc/plc_melsec.py
index 2ca1e10..fb609ce 100644
--- a/backend/plc/plc_melsec.py
+++ b/backend/plc/plc_melsec.py
@@ -105,6 +105,26 @@ class MelsecPLC(PLCInterface):
             self._connected = False
             return None
 
+    def write_signal(self, address: str, value: int) -> bool:
+        """
+        DONE/handshake registerini yozadi (P6 fix). WORD qurilma (D/W/R...) bo'lsa
+        batchwrite_wordunits; BIT qurilma (M/Y/B...) bo'lsa batchwrite_bitunits.
+        Xato bo'lsa False (ulanish keyingi siklда tiklanadi).
+        """
+        if self._mc is None or not address:
+            return False
+        try:
+            with self._lock:
+                if _is_word_device(address):
+                    self._mc.batchwrite_wordunits(headdevice=address, values=[int(value)])
+                else:
+                    self._mc.batchwrite_bitunits(headdevice=address,
+                                                 values=[1 if int(value) else 0])
+            return True
+        except Exception as exc:
+            log.warning(f"PLC yozish xatosi ({address}={value}): {exc}")
+            return False
+
     def close(self) -> None:
         with self._lock:
             if self._mc is not None:
diff --git a/backend/plc/plc_service.py b/backend/plc/plc_service.py
index b77fb87..7341184 100644
--- a/backend/plc/plc_service.py
+++ b/backend/plc/plc_service.py
@@ -49,12 +49,17 @@ class PLCService:
         self._running = False
         # 0 = idle deb boshlanadi -> startup da signal 0 bo'lsa STOP chaqirilmaydi,
         # lekin startup da signal allaqachon 1 bo'lsa START chaqiriladi.
-        self._last_signal: Optional[int] = 0
+        self._last_signal: Optional[int] = 0   # oxirgi XOM qiymat (status/log uchun)
+        # P1 FIX: edge detektsiyasi XOM word emas, hisoblangan HOLAT (0/1) ustidan.
+        self._last_state: int = 0
+        # P6 FIX: "arm" bayrog'i. Trigger ishlagach disarm bo'ladi; signal FIZIK 0 ga
+        # tushgachgina qayta arm bo'ladi -> auto-off dan keyin re-trigger bo'roni yo'q.
+        self._armed: bool = True
         self._connected = False
         self._last_read_ts: float = 0.0      # oxirgi muvaffaqiyatli o'qish (last packet)
-        # One-shot: VIN o'qilgach signalni 0 ga majburlash bayrog'i (haqiqiy PLC uchun).
-        # Simulyatorda signal to'g'ridan-to'g'ri 0 ga o'rnatiladi.
-        self._force_off = False
+        # One-shot teardown (haqiqiy PLC): VIN/sessiya tugagach kamera o'chirish +
+        # handshake DONE yozish poll THREADida bajariladi (OCR threadi bloklanmaydi).
+        self._pending_teardown = False
 
     # ---------------------------------------------------------------
     def start(self) -> None:
@@ -95,12 +100,17 @@ class PLCService:
     # shuning uchun OCR threadi bloklanmaydi.
     # ---------------------------------------------------------------
     def notify_vin_done(self) -> None:
+        # Sessiya yakunlanganda chaqiriladi (SUCCESS, qisman yoki timeout) — VIN
+        # o'qilganligini ANGLATMAYDI. Log neytral.
         if isinstance(self.plc, PLCSimulator):
-            self.plc.set_state(0)          # signal 0 -> poll keyingi sikl da STOP qiladi
-            log.info("VIN o'qildi -> PLC simulyator signali 0 (kamera o'chadi).")
+            self.plc.set_state(0)          # signal 0 -> poll keyingi sikl da STOP + re-arm
+            log.info("Sessiya yakunlandi -> PLC simulyator signali 0 (kamera o'chadi).")
         else:
-            self._force_off = True         # haqiqiy PLC: poll loop 0 deb qabul qiladi
-            log.info("VIN o'qildi -> PLC mahalliy STOP majburlandi (kamera o'chadi).")
+            # P6 FIX: lokal 0 sintez QILMAYMIZ (u soxta ko'tarilish qirrasi yaratardi).
+            # O'rniga: disarm + handshake DONE + teardown poll threadida bajariladi.
+            self._pending_teardown = True
+            log.info("Sessiya yakunlandi -> PLC teardown navbatga qo'yildi "
+                     "(disarm + DONE handshake, kamera o'chadi).")
 
     def status(self) -> dict:
         return {
@@ -136,16 +146,25 @@ class PLCService:
                 self._connected = False
                 continue
             self._last_read_ts = time.time()  # muvaffaqiyatli o'qish (last packet)
+            self._last_signal = value         # xom qiymat (status/log)
+
+            # 2b) One-shot teardown (haqiqiy PLC): disarm + DONE handshake + STOP.
+            #     Soxta 0 sintez qilinmaydi -> re-trigger bo'roni yo'q (P6).
+            if self._pending_teardown:
+                self._pending_teardown = False
+                self._armed = False
+                self._write_done()
+                # Joriy fizik holatni sinxronlaymiz (qayta start qo'zg'almasin)
+                self._last_state = self._compute_trigger_state(value)
+                try:
+                    self._on_stop()
+                except Exception as exc:
+                    log.error(f"PLC teardown on_stop xatosi: {exc}")
+                self._stop_event.wait(interval)
+                continue
 
-            # 2b) One-shot majburiy o'chirish (haqiqiy PLC): VIN o'qilgach 0 deb qabul
-            if self._force_off:
-                self._force_off = False
-                value = 0
-
-            # 3) Level-edge: holat o'zgarganda start/stop
-            if value != self._last_signal:
-                self._handle_transition(value)
-                self._last_signal = value
+            # 3) Holat (bit/mask) o'zgarganda start/stop (P1: word emas, bit/holat)
+            self._handle_transition(value)
 
             self._stop_event.wait(interval)
 
@@ -156,15 +175,72 @@ class PLCService:
             log.error(f"PLC connect xatosi: {exc}")
             return False
 
+    # ---------------------------------------------------------------
+    def _compute_trigger_state(self, value: int) -> int:
+        """
+        P1 FIX: XOM register qiymatidan (word bo'lishi mumkin) trigger HOLATINI
+        (0/1) hisoblaydi. signal_kind ga ko'ra:
+          value -> butun qiymat == trigger_on_value (haqiqiy BIT qurilma uchun)
+          bit   -> word ichidan signal_bit indeksli bit
+          mask  -> (value & trigger_mask) == (trigger_on_value & trigger_mask)
+        """
+        try:
+            v = int(value)
+        except Exception:
+            return 0
+        kind = (getattr(PLC, "signal_kind", "auto") or "auto").lower()
+        bit = int(getattr(PLC, "signal_bit", -1))
+        mask = int(getattr(PLC, "trigger_mask", 0))
+        if kind == "auto":
+            if mask:
+                kind = "mask"
+            elif bit >= 0:
+                kind = "bit"
+            else:
+                kind = "value"
+        if kind == "bit" and bit >= 0:
+            return 1 if ((v >> bit) & 1) else 0
+        if kind == "mask" and mask:
+            return 1 if (v & mask) == (int(PLC.trigger_on_value) & mask) else 0
+        # value rejimi (default) — eski xulq (BIT qurilma 0/1 qaytaradi)
+        return 1 if v == int(PLC.trigger_on_value) else 0
+
+    def _write_done(self) -> None:
+        """P6 FIX: handshake — PLC ga DONE bitini yozadi (ARRIVE ni 0 ga tushirish uchun)."""
+        addr = (getattr(PLC, "done_address", "") or "").strip()
+        if not addr:
+            return
+        try:
+            ok = self.plc.write_signal(addr, int(getattr(PLC, "done_value", 1)))
+            if ok:
+                log.info(f"PLC handshake: DONE -> {addr}={PLC.done_value} (ARRIVE ni kutamiz).")
+        except Exception as exc:
+            log.warning(f"PLC DONE handshake xatosi: {exc}")
+
     def _handle_transition(self, value: int) -> None:
-        if value == PLC.trigger_on_value:
-            log.info(f"PLC signal -> {value}: ishlov berish BOSHLANADI.")
-            try:
-                self._on_start()
-            except Exception as exc:
-                log.error(f"PLC on_start xatosi: {exc}")
-        else:
-            log.info(f"PLC signal -> {value}: ishlov berish TO'XTAYDI (idle).")
+        """
+        Holat (0/1) qirrasi bo'yicha start/stop. P1: word emas, hisoblangan holat.
+        P6: arm bayrog'i — trigger ishlagach disarm; signal 0 ga tushgachgina re-arm.
+        """
+        state = self._compute_trigger_state(value)
+        if state == self._last_state:
+            return                              # holat o'zgarmadi -> hech narsa
+        self._last_state = state
+
+        if state == 1:                          # ko'tarilish qirrasi (ARRIVE)
+            if self._armed or not getattr(PLC, "require_zero_before_rearm", True):
+                self._armed = False             # bu triggerni "iste'mol" qildik
+                log.info(f"PLC trigger (raw={value}) -> ishlov berish BOSHLANADI.")
+                try:
+                    self._on_start()
+                except Exception as exc:
+                    log.error(f"PLC on_start xatosi: {exc}")
+            else:
+                log.info(f"PLC ko'tarilish qirrasi (raw={value}) e'tiborsiz — disarmed "
+                         f"(signal 0 ga tushishini kutmoqda).")
+        else:                                   # tushish qirrasi (signal 0)
+            self._armed = True                  # fizik 0 -> qayta arm
+            log.info(f"PLC signal 0 (raw={value}) -> ishlov berish TO'XTAYDI (idle, re-armed).")
             try:
                 self._on_stop()
             except Exception as exc:
diff --git a/backend/plc/plc_simulator.py b/backend/plc/plc_simulator.py
index fc263e1..dccf9e9 100644
--- a/backend/plc/plc_simulator.py
+++ b/backend/plc/plc_simulator.py
@@ -22,6 +22,11 @@ class PLCSimulator(PLCInterface):
     def __init__(self, initial: int = 0) -> None:
         self._state = 1 if int(initial) else 0
         self._lock = threading.Lock()
+        # P5 FIX: qisqa pulslarni LATCH qilish. Haqiqiy liniyada ARRIVE biti PLC
+        # tomonda latch qilinadi (mashina o'tguncha 1 da turadi). Simulyator shu
+        # xulqni modellashtiradi: poll davridan qisqa puls (1->0) o'tkazib
+        # yuborilmasin — ko'tarilish qirrasi keyingi o'qishda bir marta ko'rsatiladi.
+        self._rising_latched = False
 
     def connect(self) -> bool:
         return True
@@ -33,11 +38,19 @@ class PLCSimulator(PLCInterface):
     def set_state(self, value: int) -> None:
         """UI 'PLC ON/OFF' tugmalari shu yerga yozadi."""
         with self._lock:
-            self._state = 1 if int(value) else 0
+            v = 1 if int(value) else 0
+            if v == 1 and self._state == 0:
+                self._rising_latched = True   # ko'tarilish qirrasini eslab qolamiz
+            self._state = v
 
     def read_signal(self) -> Optional[int]:
         with self._lock:
-            return self._state
+            if self._state == 1:
+                return 1
+            if self._rising_latched:          # o'tkazib yuborilgan qisqa pulsni qaytaramiz
+                self._rising_latched = False
+                return 1
+            return 0
 
     def close(self) -> None:
         pass
diff --git a/config/settings.yaml b/config/settings.yaml
index e422851..d1ec5b3 100644
--- a/config/settings.yaml
+++ b/config/settings.yaml
@@ -21,9 +21,21 @@ plc:
   ip: "10.123.40.99"       # Mitsubishi Q-PLC IP (melsec rejimi)
   port: 5003                # MC protokol porti
   plc_type: "Q"             # pymcprotocol plctype
-  signal_address: "D521"   # kuzatiladigan bit register
-  trigger_on_value: 1       # shu qiymat = "ishlov ber"
-  poll_interval_ms: 500     # polling davri (ms)
+  signal_address: "D521"   # kuzatiladigan register (ARRIVE)
+  trigger_on_value: 1       # shu qiymat = "ishlov ber" (value/mask rejimida)
+  # --- P1 FIX: WORD registerdan trigger bitini ajratish ---
+  # D521 WORD register (mas. 8720 qaytaradi). ARRIVE odatda BITTA bit.
+  #   signal_kind: "bit"  + signal_bit: 0   -> D521.0 ni kuzatadi (TAVSIYA, bitни tasdiqlang)
+  #   signal_kind: "mask" + trigger_mask: 1 -> (value & 1)
+  #   signal_kind: "value"                  -> butun word == trigger_on_value (haqiqiy BIT qurilma)
+  signal_kind: "bit"        # MUHIM: D521 ARRIVE bitini PLC muhandisi bilan tasdiqlang
+  signal_bit: 0             # word ichidagi bit indeksi (D521.0)
+  trigger_mask: 0
+  # --- P6 FIX: handshake (re-trigger bo'ronini to'xtatish) ---
+  done_address: ""          # mas. "D522"/"M520" — VIN tugagach DONE yoziladi (bo'sh = o'chiq)
+  done_value: 1
+  require_zero_before_rearm: true  # sessiyadan keyin signal 0 ga tushmaguncha qayta trigger yo'q
+  poll_interval_ms: 100     # P5: polling davri (500->100, qisqa pulslar uchun)
   reconnect_max_delay_sec: 30.0
   full_stop_on_zero: true   # PLC=0 -> kamera UZILADI (PLC = kamera ON/OFF tugmasi)
   auto_off_on_vin: true     # VIN o'qilgach signal 0 ga tushadi, kamera o'chadi (one-shot)
@@ -64,6 +76,8 @@ rfid:
 # qayta urinadi. Timeout bo'lsa ham natija HAR DOIM bazaga yoziladi.
 session:
   timeout_sec: 90           # maksimal sessiya davomiyligi (1 daqiqa 30 soniya)
+  require_rfid: true        # true -> SUCCESS uchun RFID ham kerak; false -> VIN yetarli
+  rfid_grace_sec: 15        # VIN topilgach RFID ni yana shuncha s kutamiz (90s ushlamaymiz)
 
 # ---------- AI: aniqlash (YOLOv8n) ----------
 detection:
@@ -90,6 +104,7 @@ vin:
   enabled: true
   default_model: "QY"
   min_final_score: 0.55
+  require_known_model: true  # true -> model=None (QY/BL7M emas) VIN RAD etiladi (productionda tavsiya)
 
 # ---------- Server / ilova ----------
 server:
```
