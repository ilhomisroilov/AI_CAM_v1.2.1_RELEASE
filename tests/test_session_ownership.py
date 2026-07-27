"""
============================================================
test_session_ownership.py — P0-2 fix: sessiya egaligi (ownership)
============================================================
Audit topilmasi: `_on_ocr_result`/`_on_rfid_result` faqat "sessiya faolmi"
degan BOOL flagni tekshirar edi — natija QAYSI session_id ga tegishli ekani
HECH QACHON solishtirilmasdi. Oqibat: eski kuzov VIN'i yangi sessiyaga
yozilishi, yoki bir xil VIN ikki BOSHQA sessiyada (noto'g'ri) dedup qilinishi
mumkin edi.

Bu fayl tekshiradi:
  4. Session A ning OCR natijasi Session B ga YOZILMAYDI.
  5. Session A ning RFID natijasi Session B ga YOZILMAYDI.
  9. Bir xil VIN ikki BOSHQA sessiyada qonuniy qabul qilinadi.
 11. RFID busy holati sessiyada ANIQ failure_reason bilan qayd etiladi
     (P1 fix — chaqiruvchi endi qaytish qiymatini tekshiradi).

Hech qanday real qurilmaga ulanish yo'q.
"""
from __future__ import annotations

from datetime import datetime

from backend import config as cfg
from backend.rfid.rfid_service import RFIDResult

from conftest import FakeRFIDService, make_ocr_result


def test_ocr_result_session_a_not_attributed_to_session_b(pipeline_instance):
    """4) Session A OCR natijasi Session B ga yozilmasin."""
    p = pipeline_instance
    p.plc_on()
    session_a = p._active_session_id

    # Session A ni majburan yakunlaymiz (masalan tashqi TIMEOUT) — endi u TERMINAL.
    p._finalize_session(session_a, reason="TEST_FORCE_CLOSE")
    assert p._sessions[session_a].state.value in ("TIMEOUT",)

    # Navbat bo'sh bo'lgani uchun sessiya faol emas — yangi trigger B ni boshlaydi.
    p.plc_on()
    session_b = p._active_session_id
    assert session_b is not None and session_b != session_a

    # Session A ga tegishli (KECH kelgan) OCR natijasi keladi.
    p._on_ocr_result(make_ocr_result(session_a, vin="WRONGVIN000000001"))

    assert p._sessions[session_b].vin is None, \
        "Session A natijasi Session B ga yozilmasligi kerak"
    assert p._metrics["late_ocr_results_total"] >= 1

    # Session B ga tegishli TO'G'RI natija esa qabul qilinadi.
    p._on_ocr_result(make_ocr_result(session_b, vin="NSTFA814ATJ123456"))
    assert p._sessions[session_b].vin == "NSTFA814ATJ123456"


def test_rfid_result_session_a_not_attributed_to_session_b(pipeline_instance, monkeypatch):
    """5) Session A RFID natijasi Session B ga yozilmasin."""
    monkeypatch.setattr(cfg.RFID, "enabled", True)
    p = pipeline_instance
    # "busy" xatti-harakati -> trigger_read on_done ni AVTOMATIK chaqirmaydi,
    # shunda natijalarni QO'LDA, aniq session_id bilan yuborib ownership ni
    # nazorat qilamiz.
    p.rfid_service = FakeRFIDService(behavior="busy")

    p.plc_on()
    session_a = p._active_session_id
    p._finalize_session(session_a, reason="TEST_FORCE_CLOSE")

    p.plc_on()
    session_b = p._active_session_id
    assert session_b != session_a

    stale = RFIDResult(session_id=session_a, epc="0000001111", number="1111",
                       antenna=1, rssi=-30.0, first_seen_at=datetime.now(), received_at=datetime.now())
    p._on_rfid_result(stale)
    assert p._sessions[session_b].rfid_epc is None, \
        "Session A RFID natijasi Session B ga yozilmasligi kerak"
    assert p._metrics["late_rfid_results_total"] >= 1

    fresh = RFIDResult(session_id=session_b, epc="0000002222", number="2222",
                       antenna=1, rssi=-30.0, first_seen_at=datetime.now(), received_at=datetime.now())
    p._on_rfid_result(fresh)
    assert p._sessions[session_b].rfid_epc == "2222"


def test_same_vin_accepted_in_two_different_sessions(pipeline_instance):
    """9) Bir xil VIN ikki BOSHQA sessiyada qonuniy qabul qilinadi (dedup sessiyaga bog'liq)."""
    p = pipeline_instance
    p.plc_on()
    session_a = p._active_session_id
    p._on_ocr_result(make_ocr_result(session_a, vin="NSTFA814ATJ123456"))
    assert p._sessions[session_a].vin == "NSTFA814ATJ123456"
    assert p._sessions[session_a].state.value == "SUCCESS"   # RFID.enabled=False -> VIN yetarli

    p.plc_on()
    session_b = p._active_session_id
    assert session_b != session_a
    p._on_ocr_result(make_ocr_result(session_b, vin="NSTFA814ATJ123456"))   # AYNAN bir xil VIN
    assert p._sessions[session_b].vin == "NSTFA814ATJ123456"
    assert p._sessions[session_b].state.value == "SUCCESS"


def test_rfid_busy_recorded_with_failure_reason_on_session(pipeline_instance, monkeypatch):
    """11) RFID busy holati sessiyada ANIQ failure_reason bilan qayd etiladi (P1 fix)."""
    monkeypatch.setattr(cfg.RFID, "enabled", True)
    p = pipeline_instance
    fake = FakeRFIDService(behavior="busy")
    p.rfid_service = fake

    p.plc_on()
    sid = p._active_session_id

    assert fake.calls == [sid], "trigger_read chaqirilishi kerak edi"
    assert p._sessions[sid].failure_reason == "RFID_BUSY", \
        "RFID band bo'lsa ANIQ failure_reason yozilishi kerak (jim tashlab qo'yilmasin)"
