"""
============================================================
tests/test_plc_pulse_latching.py — P0-3 audit fix: "qisqa impuls" muammosi
============================================================
Audit topilmasi (plc_service.py:118-150, _loop): level-polling
(poll_interval_ms~=500ms), edge-latch YO'Q -> qisqa impuls (poll oralig'idan
qisqaroq signal) YO'QOLISHI mumkin.

MUHIM (hisobotda ham yozilgan): polling tezligini oshirish BU MUAMMONI
UMUMAN HAL QILMAYDI — agar signal poll oralig'idan chindan ham qisqaroq
bo'lib, hech qanday hardware xotira (latch/seal-in) bilan ushlanmasa,
DASTURIY hech qanday hiyla uni "ko'ra olmaydi" (namunalash nazariyasi:
namunadan qisqaroq hodisa har doim yo'qolishi mumkin). Yagona ISHONCHLI
yechim — PLC/ladder tomonida signalni ACK/keyingi pollgacha LATCH qilish
(yoki alohida handshake/sequence registri). Bu fayl aynan shu kontraktni
(PLCSimulator.pulse()) va PLCService uni qanday to'g'ri iste'mol qilishini
tekshiradi — bu ANIQ FAKT emas, HIL da controls muhandisi bilan
tasdiqlanishi kerak bo'lgan TALAB sifatida hujjatlashtiriladi.

Hech qanday real qurilmaga ulanish yo'q.
"""
from __future__ import annotations

import backend.config as cfg
from backend.plc.plc_service import PLCService
from backend.plc.plc_simulator import PLCSimulator


def _make_service(monkeypatch, sim: PLCSimulator):
    monkeypatch.setattr(cfg.PLC, "require_acknowledgement", False)
    monkeypatch.setattr(cfg.PLC, "done_address", None)
    monkeypatch.setattr(cfg.PLC, "write_enabled", False)
    monkeypatch.setattr(cfg.PLC, "trigger_on_value", 1)
    monkeypatch.setattr(cfg.PLC, "startup_recovery_policy", "wait_for_edge")

    starts, stops = [], []
    svc = PLCService(on_start=lambda: starts.append(1), on_stop=lambda: stops.append(1))
    svc.plc = sim
    svc._connected = True
    # Startup-siyosat testlaridan mustaqil qilish uchun "birinchi poll"ni 0
    # bilan iste'mol qilamiz (signal off holatda ishga tushgandek).
    svc._poll_once()
    assert starts == [] and stops == []
    return svc, starts, stops


# ============================================================
# 5) Qisqa pulse — LATCH bilan yo'qolmaydi (poll tezligidan qat'i nazar)
# ============================================================
def test_short_latched_pulse_not_lost(monkeypatch):
    """
    PLCSimulator.pulse() — signalni "seal-in" (latch) uslubida ko'taradi: u
    BIRINCHI o'qishgacha 1 bo'lib qoladi, keyin AVTOMATIK 0 ga tushadi. Bu —
    poll qanchalik kam-tez chaqirilishidan qat'i nazar, bitta to'liq trigger
    siklini (ON -> BUSY -> OFF) YO'QOTMASDAN ushlab qolish kafolatini beradi.

    Test QASDDAN ko'p marta (10x) _poll_once() chaqiradi — real PLC bo'lganda
    bu turlicha tezlikda ishlashi mumkin, LEKIN natija HAR DOIM bir xil:
    on_start FAQAT bir marta (pulse "ko'rilganda"), keyin on_stop (avtomatik
    tushgan on).
    """
    sim = PLCSimulator(initial=0)
    svc, starts, stops = _make_service(monkeypatch, sim)

    sim.pulse()

    # 10 marta ketma-ket poll qilamiz — pulse bir marta "iste'mol" qilingach
    # signal 0 ga tushadi va qolgan pollarda hech narsa o'zgarmaydi.
    for _ in range(10):
        svc._poll_once()

    assert len(starts) == 1, "latched qisqa impuls yo'qolmasligi kerak (aynan bitta trigger)"
    # Production `trigger_mode=pulse`: falling edge faqat fizik impuls tugaganini
    # bildiradi; faol sessiya VIN yoki hard deadline'gacha davom etadi.
    assert len(stops) == 0
    assert svc.status()["handshake_state"] == "BUSY"


def test_multiple_latched_pulses_each_produce_one_session(monkeypatch):
    """Ketma-ket ikkita mustaqil latched pulse — ikkita ALOHIDA trigger sikli."""
    sim = PLCSimulator(initial=0)
    svc, starts, stops = _make_service(monkeypatch, sim)

    sim.pulse()
    svc._poll_once()   # ON ko'rildi -> on_start, signal avtomatik 0 ga tushadi
    svc._poll_once()   # signal 0 -> on_stop (allaqachon IDLE bo'lgani uchun ikkinchi marta chaqirilmaydi)

    sim.pulse()
    svc._poll_once()

    assert len(starts) == 2, "har bir mustaqil latched pulse ALOHIDA trigger bo'lishi kerak"
    assert len(stops) == 0, ("pulse falling edge sessiyani yopmaydi; har ikkala triggerni "
                             "pipeline active/queue siyosati boshqaradi")


def test_unlatched_naive_polling_can_miss_pulse_shorter_than_interval():
    """
    HUJJATLASH uchun (kod o'zgarmaydi, faqat nazariyani tasdiqlaydi):
    agar signal poll chaqirilishidan OLDIN va KEYIN o'qilmasdan o'zi 0 ga
    qaytsa (ya'ni hech qanday PLC-tomon latch bo'lmasa), _poll_once() bu
    hodisani UMUMAN KO'RMAYDI — chunki read_signal() shunchaki joriy holatni
    qaytaradi, tarixni emas. Bu — nega yechim "polling tezligini oshirish"
    EMAS, balki "PLC-tomon latch" ekanini isbotlaydi.
    """
    sim = PLCSimulator(initial=0)
    sim.set_state(1)     # "impuls" boshlandi
    sim.set_state(0)     # ...va HECH KIM o'qimasdan darhol tugadi (naive, latch YO'Q)
    # Poll shu payt keladi (real PLC yoki simulyator farq qilmaydi):
    assert sim.read_signal() == 0, ("latch bo'lmasa, poll chaqirilganda impuls "
                                    "allaqachon o'tib ketgan bo'lishi mumkin — "
                                    "bu FIZIK cheklov, dasturiy tuzatib bo'lmaydi")
