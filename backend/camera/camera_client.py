"""
SICK Lector 652 - Camera Client  (STRICT BLOB protokol)
=========================================================================
Tarmoq logikasi `lector652_pipeline` loyihasining ISHLAYDIGAN kodiga
asoslangan va quyidagi qat'iy qoidalar bilan mustahkamlangan:

  1. Mustahkam socket: SO_RCVBUF=16MB, setblocking(True) + SO_RCVTIMEO=2s
     (select() ISHLATILMAYDI), dinamik IP, SO_LINGER (RST on close).
  2. _recv_exactly(num_bytes): recv_into() while-loop — TCP fragmentatsiyada
     bayt yo'qolmaydi.
  3. ANIQ paket ketma-ketligi (live, mLIStart 0):
        4B  Magic (BLOB_STX = 02 02 02 02)
        4B  Payload uzunligi (Big-Endian, >I)
        NB  Payload
        1B  Checksum
  4. Dekodlash (AYLANTIRISH YO'Q):
        - Payload boshida 19 baytlik SICK sub-header -> payload[19:]
        - np.frombuffer(..., uint8) + cv2.imdecode(..., IMREAD_GRAYSCALE)
        - Xom (aylantirilmagan) matritsa qaytariladi.

CoLa A protokoli port 2111, BLOB port 2113.
"""

from __future__ import annotations

import errno
import socket
import struct
import sys
import threading
import time
import logging
from typing import Callable, Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)

DEFAULT_CONTROL_PORT = 2111
DEFAULT_BLOB_PORT    = 2113
DEFAULT_PASSWORD     = ""

_SOCK_BUFSIZE        = 16 * 1024 * 1024   # 16 MB TCP receive buffer
_BLOB_RCV_TIMEOUT    = 2.0                # SO_RCVTIMEO (sekund)

# Live BLOB framing
BLOB_STX             = b"\x02\x02\x02\x02"   # 4 baytlik magic
SICK_SUBHEADER_LEN   = 19                    # payload boshidagi sub-header
_MAX_PAYLOAD         = 50 * 1024 * 1024      # 50 MB sanity cheklov
_MAX_RESYNC_SCAN     = 8 * 1024 * 1024       # desync da STX qidirish chegarasi

# recv() timeout sifatida talqin qilinadigan OS errno lar
_TIMEOUT_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, errno.ETIMEDOUT, 10060}  # 10060=WSAETIMEDOUT


# -- CoLa A (control kanal, port 2111) ---------------------------------------

def _cola_pack(cmd: str) -> bytes:
    return b"\x02" + cmd.encode("ascii") + b"\x03"


def _cola_expected_ack(cmd: str) -> tuple[str, str]:
    """CoLa request uchun kutiladigan (javob_turi, buyruq_nomi) ni qaytaradi."""
    parts = cmd.split()
    if len(parts) < 2:
        raise ValueError("Noto'g'ri CoLa buyruq: {!r}".format(cmd))
    response_type = {"sMN": "sAN", "sRN": "sRA", "sEN": "sEA"}.get(parts[0])
    if response_type is None:
        raise ValueError("Qo'llab-quvvatlanmagan CoLa buyruq turi: {!r}".format(parts[0]))
    return response_type, parts[1]


def _cola_recv(
    sock: socket.socket,
    timeout: float = 5.0,
    expected_ack: tuple[str, str] | None = None,
    on_unmatched: Callable[[str], None] | None = None,
) -> str:
    """
    Control socketdan bitta CoLa javob o'qiydi.

    ``expected_ack`` berilganda faqat aynan shu requestga tegishli ACK qaytadi.
    Kamera shu kanalda yuboradigan async measurement reportlari, ``sSN`` eventlar
    va kechikib kelgan eski ACKlar deadline ichida tashlab o'tiladi. Bu eski
    ``mLIStop`` javobining keyingi ``mLIStart`` holatini zaharlashiga yo'l
    qo'ymaydi.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    buf = bytearray()
    unmatched_count = 0
    while True:
        while b"\x03" in buf:
            end  = buf.index(0x03) + 1
            raw  = bytes(buf[:end])
            buf  = bytearray(buf[end:])
            text = raw.lstrip(b"\x02").rstrip(b"\x03").decode("latin1", errors="replace").strip()
            if not text:
                continue
            if expected_ack is None:
                if text.startswith("sSN"):
                    log.debug("sSN skip: %s", text[:80])
                    continue
                return text

            parts = text.split()
            if len(parts) >= 2 and (parts[0], parts[1]) == expected_ack:
                return text
            # Kamera CoLa failure javobini aynan shu command uchun qaytarsa,
            # deadline kutishning foydasi yo'q: haqiqiy command rejection.
            if parts and parts[0] == "sFA" and (
                len(parts) < 2 or parts[1] == expected_ack[1]
            ):
                raise RuntimeError("CoLa buyruq rad etildi: {}".format(text))
            if on_unmatched is not None:
                on_unmatched(text)
            unmatched_count += 1
            # Kamera control kanaliga bir soniyada yuzlab measurement telegram
            # yuborishi mumkin. Birinchi namunalar diagnostika uchun yetarli;
            # qolganlarini har bittasini diskka yozish production logni shishiradi.
            if unmatched_count <= 3:
                log.debug("CoLa mos kelmagan telegram skip (expected=%s %s): %s",
                          expected_ack[0], expected_ack[1], text[:160])

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            expected = "{} {}".format(*expected_ack) if expected_ack else "CoLa javob"
            raise TimeoutError("{} uchun mos ACK kelmadi ({}s)".format(expected, timeout))
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(4096)
        except (socket.timeout, TimeoutError) as exc:
            expected = "{} {}".format(*expected_ack) if expected_ack else "CoLa javob"
            raise TimeoutError("{} uchun mos ACK kelmadi ({}s)".format(
                expected, timeout)) from exc
        if not chunk:
            raise ConnectionError("Control socket yopildi")
        buf.extend(chunk)


# -- Socket sozlash ----------------------------------------------------------

def _apply_rcvtimeo(sock: socket.socket, timeout_sec: float) -> None:
    """SO_RCVTIMEO ni OS darajasida o'rnatadi (Python select() siz)."""
    if sys.platform == "win32":
        # Windows: DWORD millisekund
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO,
                        struct.pack("I", int(timeout_sec * 1000)))
    else:
        # Linux / macOS: struct timeval (sec, usec)
        sec  = int(timeout_sec)
        usec = int((timeout_sec - sec) * 1_000_000)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO,
                        struct.pack("ll", sec, usec))


def _configure_blob_socket(sock: socket.socket) -> None:
    """BLOB socketga talab qilingan mustahkam sozlamalar."""
    # 16 MB qabul buferi (yuqori fps da fragmentatsiyani kamaytiradi)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _SOCK_BUFSIZE)
    # RST on close — kamera bitta ulanish qabul qiladi, darhol bo'shatiladi
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    # Bloklovchi rejim + OS-level 2s timeout (select() ISHLATILMAYDI)
    sock.setblocking(True)
    _apply_rcvtimeo(sock, _BLOB_RCV_TIMEOUT)


# -- Qat'iy bayt o'qish ------------------------------------------------------

def _recv_exactly(sock: socket.socket, num_bytes: int) -> bytes:
    """
    Socketdan ANIQ num_bytes o'qiydi (TCP fragmentatsiyaga chidamli).

    recv_into(view, remaining) while-loopda — bironta bayt yo'qolmaydi.
    SO_RCVTIMEO (2s) tufayli recv bloklanib qolmaydi; timeoutda TimeoutError.
    """
    if num_bytes <= 0:
        return b""
    buf  = bytearray(num_bytes)
    view = memoryview(buf)
    pos  = 0
    while pos < num_bytes:
        try:
            n = sock.recv_into(view[pos:], num_bytes - pos)
        except socket.timeout as exc:                       # settimeout bo'lsa
            raise TimeoutError("recv timeout ({}/{} bayt)".format(pos, num_bytes)) from exc
        except OSError as exc:
            if exc.errno in _TIMEOUT_ERRNOS:                # SO_RCVTIMEO timeout
                raise TimeoutError("recv timeout ({}/{} bayt)".format(pos, num_bytes)) from exc
            raise ConnectionError("recv xatosi: {}".format(exc)) from exc
        if n == 0:
            raise ConnectionError("Socket yopildi ({}/{} bayt)".format(pos, num_bytes))
        pos += n
    return bytes(buf)


def _resync_to_stx(sock: socket.socket, window: bytes) -> None:
    """
    Desync holatida oqimni BLOB_STX (02 02 02 02) ga qayta moslaydi.
    `window` — endigina o'qilgan, lekin STX bo'lmagan 4 bayt.
    4 baytlik oynani 1 baytdan suradi, magic topilguncha.
    """
    win = bytearray(window)
    scanned = 0
    while bytes(win) != BLOB_STX:
        win.pop(0)
        win.extend(_recv_exactly(sock, 1))
        scanned += 1
        if scanned > _MAX_RESYNC_SCAN:
            raise ValueError("BLOB_STX topilmadi — oqim desync (resync chegarasi).")
    log.warning("Desync: %d bayt o'tkazib STX ga qayta moslandi.", scanned)


def _read_blob_frame(sock: socket.socket) -> bytes:
    """
    ANIQ ketma-ketlik bo'yicha bitta live BLOB frame o'qiydi va PAYLOAD ni
    qaytaradi (sub-header bilan birga; checksum tashlanadi).

        4B  Magic     (BLOB_STX)
        4B  Length    (Big-Endian, >I)
        NB  Payload
        1B  Checksum  (o'qiladi va tashlanadi)
    """
    # 1) Magic (4B)
    magic = _recv_exactly(sock, 4)
    if magic != BLOB_STX:
        _resync_to_stx(sock, magic)        # desyncdan tiklanish

    # 2) Payload uzunligi (4B, Big-Endian unsigned int)
    (length,) = struct.unpack(">I", _recv_exactly(sock, 4))
    if length <= 0 or length > _MAX_PAYLOAD:
        raise ValueError("Noto'g'ri payload uzunligi: {}".format(length))

    # 3) Payload (NB)
    payload = _recv_exactly(sock, length)

    # 4) Checksum (1B) — o'qiladi, lekin ishlatilmaydi (stream sinxron qoladi)
    _recv_exactly(sock, 1)

    return payload


# -- Payload -> tasvir (ROBUST multi-format dekod, AYLANTIRISH YO'Q) ----------
#
# Kamera "BM" (BMP) uzatishi kerak, lekin payload formati har doim
# "19 bayt sub-header + BMP" bo'lishi SHART EMAS. Ba'zan oldida XML/report
# bo'ladi yoki sub-header uzunligi farq qiladi. Shuning uchun payload ichidan
# HAQIQIY image magic'larni topamiz va eng ishonchli candidate'ni dekod qilamiz.

# Image magic'lar (BMP ustuvor — kamera BMP uzatadi)
_MAGIC_BMP  = b"BM"
_MAGIC_JPEG = b"\xff\xd8\xff"
_MAGIC_PNG  = b"\x89PNG\r\n\x1a\n"

# Log spamini oldini olish uchun — format/shape o'zgarganda bir marta INFO
_last_decode_sig: Optional[tuple] = None


def _decode_bmp_8bpp(bmp: bytes) -> Optional[np.ndarray]:
    """Zaxira: 8bpp grayscale BMP ni to'g'ridan-to'g'ri numpy ga (imdecode ishlamasa)."""
    try:
        if len(bmp) < 54 or bmp[:2] != b"BM":
            return None
        px_off = int.from_bytes(bmp[10:14], "little")
        raw_w  = int.from_bytes(bmp[18:22], "little")
        raw_h_ = int.from_bytes(bmp[22:26], "little", signed=True)
        bpp    = int.from_bytes(bmp[28:30], "little")
        if bpp != 8 or raw_w <= 0 or raw_h_ == 0:
            return None
        top_down   = raw_h_ < 0
        raw_h      = abs(raw_h_)
        row_stride = ((raw_w + 3) // 4) * 4
        px_end     = px_off + row_stride * raw_h
        if px_end > len(bmp):
            return None
        arr = np.frombuffer(bmp[px_off:px_end], dtype=np.uint8)
        img = arr.reshape(raw_h, row_stride)[:, :raw_w]
        if not top_down:
            # BMP satr tartibini to'g'rilaydi (orientatsiya o'zgarmaydi).
            img = np.ascontiguousarray(img[::-1])
        return img
    except Exception:
        return None


def _find_bmp_offset(payload: bytes) -> int:
    """
    'BM' magic'ni topadi, lekin FAQAT header'i ishonchli bo'lganini (tasodifiy
    'BM' ketma-ketligi emas). BMP header: 'BM' + fileSize(4 LE) + reserved(4) +
    pixelDataOffset(4 LE). Topilmasa -1.
    """
    start = 0
    n = len(payload)
    while True:
        i = payload.find(_MAGIC_BMP, start)
        if i < 0 or i + 54 > n:
            return -1
        try:
            file_size = int.from_bytes(payload[i + 2:i + 6], "little")
            px_off    = int.from_bytes(payload[i + 10:i + 14], "little")
            dib_size  = int.from_bytes(payload[i + 14:i + 18], "little")
            avail     = n - i
            # Ishonchlilik: pixel offset header ichida, DIB header standart (12..124),
            # fayl o'lchami mavjud baytlardan oshmasligi (kichik xatolikka yo'l qo'yamiz).
            if 26 <= px_off <= avail and 12 <= dib_size <= 124 and 0 < file_size <= avail + 64:
                return i
        except Exception:
            pass
        start = i + 2
    # erishilmaydi


def _hexdump(data: bytes, length: int = 128) -> str:
    """Diagnostika uchun payload boshidan hex + ascii dump (decode fail bo'lganda)."""
    chunk = data[:length]
    lines = []
    for off in range(0, len(chunk), 16):
        row = chunk[off:off + 16]
        hexs = " ".join(f"{b:02x}" for b in row)
        asci = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"  {off:04x}  {hexs:<47}  {asci}")
    return "\n".join(lines)


def _decode_raw_gray(payload: bytes, hint_wh) -> Optional[np.ndarray]:
    """
    SICK-aware RAW grayscale fallback: container (BMP/JPEG/PNG) topilmasa, kamera
    e'lon qilgan W×H (mDIGetEffImgSize) bo'yicha xom 8bpp baytlarni reshape qiladi.

    SICK Lector odatda: [header/metadata] + [xom rasm]. Shuning uchun payload
    OXIRIDAN W*H bayt olinadi (eng ishonchli). Faqat payload kerakli o'lcham(lar)dan
    katta bo'lsa qo'llanadi -> kichik (JPEG bo'lishi mumkin) payloadlar reshape
    qilinmaydi (noto'g'ri tasvir oldini olish).
    """
    if not hint_wh:
        return None
    w, h = int(hint_wh[0] or 0), int(hint_wh[1] or 0)
    if w <= 0 or h <= 0:
        return None
    need = w * h
    n = len(payload)
    if n < need:
        return None                          # raw 8bpp uchun juda kichik (ehtimol JPEG)
    # Oxiridan W*H bayt (image header'dan keyin keladi)
    try:
        tail = payload[n - need:]
        return np.frombuffer(tail, dtype=np.uint8).reshape(h, w)
    except Exception:
        return None


def decode_payload(payload: bytes, hint_wh=None):
    """
    Live BLOB payload -> (grayscale numpy | None, info dict).  XOM, AYLANTIRISH YO'Q.

    Robust strategiya (BMP ustuvor — kamera BMP uzatadi):
      1. Payload ichidan image magic offsetlarini topadi: BMP('BM'),
         JPEG(FF D8 FF), PNG(89 50 4E 47...). XML/report prefiks bo'lsa ham
         o'tib, keyingi image section'ni topadi.
      2. Candidate'larni TARTIB bilan sinaydi: BMP -> JPEG -> PNG, plus
         orqaga moslik uchun payload[19:] va payload[0:].
      3. BMP uchun cv2.imdecode ishlamasa 8bpp raw parser fallback.
      4. Container topilmasa — SICK W×H (hint_wh) bo'yicha RAW grayscale reshape.
      5. Hech narsa decode bo'lmasa info ga 128-bayt hexdump qo'shiladi.

    hint_wh: (width, height) — kamera mDIGetEffImgSize dan (RAW fallback uchun).
    info: {len, bmp_off, jpg_off, png_off, format, source_off, shape, hexdump}
    """
    info = {"len": len(payload) if payload else 0, "bmp_off": -1, "jpg_off": -1,
            "png_off": -1, "format": None, "source_off": -1, "shape": None,
            "hexdump": None}
    if not payload or len(payload) < 4:
        info["hexdump"] = _hexdump(payload or b"")
        return None, info

    bmp_off = _find_bmp_offset(payload)
    jpg_off = payload.find(_MAGIC_JPEG)
    png_off = payload.find(_MAGIC_PNG)
    info.update(bmp_off=bmp_off, jpg_off=jpg_off, png_off=png_off)

    # Candidate ro'yxati: (format, offset). BMP ustuvor.
    candidates = []
    if bmp_off >= 0:
        candidates.append(("BMP", bmp_off))
    # Orqaga moslik: ko'p kamera "19B sub-header + BMP" yuboradi
    if len(payload) > SICK_SUBHEADER_LEN:
        candidates.append(("BMP?", SICK_SUBHEADER_LEN))
    candidates.append(("RAW", 0))
    if jpg_off >= 0:
        candidates.append(("JPEG", jpg_off))
    if png_off >= 0:
        candidates.append(("PNG", png_off))

    for fmt, off in candidates:
        if off < 0 or off >= len(payload):
            continue
        body = payload[off:]
        img = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if img is None and fmt in ("BMP", "BMP?", "RAW"):
            img = _decode_bmp_8bpp(body)         # 8bpp raw fallback
        if img is not None and getattr(img, "size", 0) > 0:
            real_fmt = ("BMP" if fmt in ("BMP", "BMP?") and body[:2] == _MAGIC_BMP else
                        "JPEG" if body[:3] == _MAGIC_JPEG else
                        "PNG" if body[:8] == _MAGIC_PNG else
                        ("BMP" if body[:2] == _MAGIC_BMP else "UNKNOWN"))
            info.update(format=real_fmt, source_off=off, shape=tuple(img.shape))
            _maybe_log_decode(info)
            return img, info

    # 4) SICK RAW grayscale fallback (container topilmadi) — kamera W×H bo'yicha
    raw = _decode_raw_gray(payload, hint_wh)
    if raw is not None and raw.size > 0:
        info.update(format="RAW8", source_off=len(payload) - raw.size, shape=tuple(raw.shape))
        _maybe_log_decode(info)
        return raw, info

    # Hech narsa decode bo'lmadi — diagnostika
    info["hexdump"] = _hexdump(payload)
    return None, info


def _maybe_log_decode(info: dict) -> None:
    """Format/shape o'zgarganda BIR MARTA INFO (har frame'da log spam bo'lmasin)."""
    global _last_decode_sig
    sig = (info.get("format"), info.get("shape"), info.get("source_off"))
    if sig != _last_decode_sig:
        _last_decode_sig = sig
        log.info("Kamera dekod: format=%s shape=%s offset=%d (payload=%d B)",
                 info.get("format"), info.get("shape"), info.get("source_off"),
                 info.get("len"))


def decode_bmp(payload: bytes, hint_wh=None) -> Optional[np.ndarray]:
    """Orqaga moslik: faqat tasvirni qaytaradi (decode_payload ustidan wrapper)."""
    img, _info = decode_payload(payload, hint_wh=hint_wh)
    return img


# -- Asosiy klient ------------------------------------------------------------

class Lector652Client:
    """
    SICK Lector 652 CoLa A (port 2111) + BLOB (port 2113) klient.

    Live streaming:
      connect() -> start_stream() -> loop: read_stream_frame() -> stop_stream() -> disconnect()
    """

    def __init__(
        self,
        ip: str,
        control_port: int = DEFAULT_CONTROL_PORT,
        blob_port: int    = DEFAULT_BLOB_PORT,
        password: str     = DEFAULT_PASSWORD,
        on_log: Callable[[str], None] | None = None,
    ):
        self.ip           = ip                       # dinamik IP (UI dan)
        self.control_port = control_port
        self.blob_port    = blob_port
        self.password     = password
        self._on_log      = on_log or log.info
        self._ctrl: socket.socket | None = None
        self._blob: socket.socket | None = None
        # Control requestlar parallel yuborilib, ACKlar bir-biriga aralashmasin.
        self._ctrl_lock = threading.RLock()
        self._live_active  = False
        self._last_recv_ms = 0.0
        # Kamera e'lon qilgan effektiv rasm o'lchami (mDIGetEffImgSize) — RAW
        # grayscale fallback uchun (container topilmasa shu W×H bilan reshape).
        self.img_width:  int = 0
        self.img_height: int = 0

    # -- Ulanish --------------------------------------------------------------

    def connect(self, timeout: float = 8.0, retries: int = 3) -> None:
        """CoLa control ulanishi + handshake + BLOB socket (dinamik IP)."""
        last_err = None
        for attempt in range(1, retries + 1):
            self._info("Control -> {}:{} (urinish {}/{})".format(
                self.ip, self.control_port, attempt, retries))
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _SOCK_BUFSIZE)
                s.settimeout(timeout)
                s.connect((self.ip, self.control_port))
                self._ctrl = s
                break
            except socket.timeout:
                last_err = ConnectionError(
                    "Kamera {}:{} javob bermadi ({}s, urinish {}/{}). "
                    "Kamera faqat bitta ulanish qabul qiladi — SOPAS ET yopiqligini tekshiring.".format(
                        self.ip, self.control_port, timeout, attempt, retries))
                if attempt < retries:
                    self._info("[!] Timeout — {}s kutib qayta urinilmoqda...".format(attempt * 3))
                    time.sleep(attempt * 3)
            except OSError as exc:
                last_err = ConnectionError("Kamera {}:{} ga ulanib bo'lmadi: {}".format(
                    self.ip, self.control_port, exc))
                break
        else:
            raise last_err

        # BUG FIX: yuqoridagi `except OSError: ... break` self._ctrl ni HECH QACHON
        # o'rnatmasdan siklni tark etardi (`else: raise` esa faqat break BO'LMASA
        # ishlaydi — break bo'lganda xato yutilib qolardi). Natijada pastdagi
        # self._ctrl.sendall(...) None ustida chaqirilib, chalkash
        # "'NoneType' object has no attribute 'sendall'" xatosini berardi —
        # asl ulanish xatosi (masalan "unreachable host") butunlay yashiringan
        # edi. Endi ulanish muvaffaqiyatsiz bo'lsa ANIQ xato ko'tariladi.
        if self._ctrl is None:
            raise last_err or ConnectionError(
                "Kamera {}:{} ga ulanib bo'lmadi (noma'lum xato).".format(
                    self.ip, self.control_port))

        # Parol (kamera commandni rad etsa davom etamiz; transport/protokol
        # timeoutida esa yarim ulanish bilan handshake'ni davom ettirmaymiz).
        try:
            self._cola("sMN CheckPassword 3 {}".format(self.password))
        except (TimeoutError, ConnectionError):
            raise
        except Exception as e:
            self._info("[!] CheckPassword: {} — davom etmoqda".format(e))

        # SOPAS ET tartibiga mos handshake ketma-ketligi
        self._cola("sRN DeviceIdent")
        self._cola("sEN DemoModeState 1")
        self._cola("sRN DemoModeState")
        self._cola("sMN GetBlobClientConfig")
        self._cola("sEN ImgBlobTransfer 1")
        self._cola("sEN LIIsActive 1")
        self._cola("sRN LIIsActive")
        self._cola("sEN VIStatDisp 1")
        self._cola("sRN VIStatDisp")
        self._cola("sEN VITmeStatDisp 1")
        self._cola("sRN VITmeStatDisp")
        resp = self._cola("sMN mDIGetEffImgSize")
        self._info("Effektiv rasm o'lchami: {}".format(resp))
        # Javobdan W H ni ajratamiz: "sAN mDIGetEffImgSize 800 440" -> (800, 440)
        try:
            parts = resp.split()
            ints = [int(p) for p in parts if p.isdigit()]
            if len(ints) >= 2:
                self.img_width, self.img_height = ints[-2], ints[-1]
                self._info("Effektiv geometriya: {}x{} (RAW grayscale fallback uchun).".format(
                    self.img_width, self.img_height))
        except Exception as exc:
            self._info("[!] EffImgSize parse: {}".format(exc))

        # BLOB socket (dinamik IP) — mustahkam sozlamalar bilan
        self._info("Blob -> {}:{}".format(self.ip, self.blob_port))
        self._blob = socket.create_connection((self.ip, self.blob_port), timeout=timeout)
        _configure_blob_socket(self._blob)
        self._info("Kamera tayyor (BLOB: 16MB buf, blocking + 2s SO_RCVTIMEO).")

    def disconnect(self) -> None:
        if self._live_active:
            try:
                self.stop_stream()
            except Exception:
                pass
        if self._ctrl:
            for cmd in ("sEN VITmeStatDisp 0", "sEN VIStatDisp 0",
                        "sEN LIIsActive 0", "sEN ImgBlobTransfer 0",
                        "sEN DemoModeState 0"):
                try:
                    self._cola(cmd)
                except Exception:
                    pass
            try:
                self._ctrl.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self._ctrl.close()
            except Exception:
                pass
            self._ctrl = None
        if self._blob:
            try:
                self._blob.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self._blob.close()
            except Exception:
                pass
            self._blob = None
        self._info("Ulanish yopildi.")

    @property
    def connected(self) -> bool:
        return self._ctrl is not None and self._blob is not None

    # -- Live streaming (mLIStart) --------------------------------------------

    def start_stream(self) -> None:
        """sMN mLIStart 0 — kamera avtomatik frame push qiladi."""
        if not self._blob or not self._ctrl:
            raise RuntimeError("Ulanish yo'q")
        self._info("Live streaming boshlanyapti (mLIStart 0)...")
        resp = self._cola_wait("sMN mLIStart 0", timeout=8.0)
        # Faqat matching ``sAN mLIStart`` kelgandan keyin live holat yoqiladi.
        self._live_active = True
        self._info("mLIStart javob: {}".format(resp))

    def stop_stream(self) -> None:
        """sMN mLIStop — live streamingni to'xtatadi."""
        if not self._ctrl:
            return
        try:
            resp = self._cola_wait("sMN mLIStop", timeout=5.0)
            self._info("mLIStop javob: {}".format(resp))
        except Exception as e:
            self._info("[!] stop_stream: {}".format(e))
            raise
        # Faqat matching ``sAN mLIStop`` kelgandan keyin live holat o'chadi.
        self._live_active = False

    def read_stream_frame(self) -> bytes:
        """
        Bitta live frame PAYLOAD ini o'qiydi (qat'iy STX/len/payload/checksum
        ketma-ketligi). decode_bmp() bu payload boshidagi 19B sub-headerni
        tashlab tasvirga o'giradi.
        """
        if not self._blob:
            raise RuntimeError("Ulanish yo'q")
        t0 = time.monotonic()
        payload = _read_blob_frame(self._blob)
        self._last_recv_ms = (time.monotonic() - t0) * 1000.0
        return payload

    @property
    def last_recv_ms(self) -> float:
        return self._last_recv_ms

    # -- Ichki (CoLa) ---------------------------------------------------------

    def _cola(self, cmd: str) -> str:
        return self._cola_exchange(cmd, timeout=5.0)

    def _cola_wait(self, cmd: str, timeout: float = 5.0) -> str:
        return self._cola_exchange(cmd, timeout=timeout)

    def _cola_exchange(self, cmd: str, timeout: float) -> str:
        expected_ack = _cola_expected_ack(cmd)
        safe_cmd = self._safe_cola_command(cmd)
        with self._ctrl_lock:
            ctrl = self._ctrl
            if ctrl is None:
                raise ConnectionError("Control socket ulanmagan")
            self._info("  C->K  {}".format(safe_cmd))
            try:
                skipped = 0

                def _report_unmatched(text: str) -> None:
                    nonlocal skipped
                    skipped += 1
                    if skipped <= 3:
                        self._info(
                            "  K->C  [skip; kutilgan {} {}] {}".format(
                                expected_ack[0], expected_ack[1],
                                self._one_line(text)[:240]))

                ctrl.sendall(_cola_pack(cmd))
                resp = _cola_recv(
                    ctrl,
                    timeout=timeout,
                    expected_ack=expected_ack,
                    on_unmatched=_report_unmatched,
                )
            except (TimeoutError, ConnectionError, OSError):
                # Matching ACK kelmasa control/BLOB juftligini yaroqsiz deb
                # belgilaymiz. Pipeline keyingi triggerda toza reconnect qiladi;
                # stale telegram keyingi commandni zaharlay olmaydi.
                self._invalidate_connection()
                raise
            if skipped > 3:
                self._info(
                    "  K->C  [skip x{}; kutilgan {} {}] oraliq telegramlar "
                    "qisqartirildi".format(
                        skipped, expected_ack[0], expected_ack[1]))
            self._info("  K->C  {}".format(resp))
            return resp

    @staticmethod
    def _safe_cola_command(cmd: str) -> str:
        """CheckPassword credentialini logdan source darajasida olib tashlaydi."""
        parts = cmd.split()
        if len(parts) >= 4 and parts[0:2] == ["sMN", "CheckPassword"]:
            return "{} {} {} ***".format(parts[0], parts[1], parts[2])
        return cmd

    @staticmethod
    def _one_line(text: str) -> str:
        return " ".join((text or "").split())

    def _invalidate_connection(self) -> None:
        """Protocol/transport xatosida socketlarni yubormasdan darhol yopadi."""
        self._live_active = False
        for attr in ("_ctrl", "_blob"):
            sock = getattr(self, attr, None)
            setattr(self, attr, None)
            if sock is None:
                continue
            try:
                sock.close()
            except Exception:
                pass

    def _info(self, msg: str) -> None:
        self._on_log(msg)
