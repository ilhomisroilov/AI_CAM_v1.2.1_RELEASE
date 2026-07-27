"""
============================================================
test_session_queue.py — P0-1 fix: trigger-loss / bounded FIFO queue
============================================================
Audit topilmasi: `pipeline.py` da `plc_on()` faol sessiya paytida kelgan
yangi PLC trigger'ni JIMGINA tashlab yuborar edi ("e'tiborsiz qoldirildi") —
ikkinchi kuzov uchun DB'da HECH QANDAY yozuv qolmasdi.

Bu fayl tekshiradi:
  1. Faol sessiya paytida ikkinchi trigger navbatga tushadi (tashlanmaydi).
  2. Uchta trigger tartibni saqlaydi (FIFO).
  3. Navbat to'lganda (overflow) — jim tashlanmaydi: CRITICAL log +
     dropped_trigger_count metrikasi oshadi.
  4. Navbatdagi trigger — oldingi sessiya tugagach (finalize) AVTOMATIK,
     tartibda boshlanadi.

Hech qanday real qurilmaga ulanish yo'q — `pipeline_instance` fixture
(tests/conftest.py) kamera/OCR ishga tushirishlarini FAKE qiladi.
"""
from __future__ import annotations

import logging

from backend import config as cfg

from conftest import make_ocr_result, wait_until


def test_second_trigger_queued_during_active_session(pipeline_instance):
    """1) Active session vaqtida ikkinchi trigger navbatga tushadi (tashlanmaydi)."""
    p = pipeline_instance
    p.plc_on()
    assert p._active_session_id is not None
    first_sid = p._active_session_id

    p.plc_on()   # ikkinchi trigger — birinchi sessiya hali faol (VIN kelmagan)

    assert p._active_session_id == first_sid, "birinchi sessiya hamon faol bo'lishi kerak"
    assert len(p._pending_triggers) == 1, "ikkinchi trigger navbatga tushishi kerak (tashlanmasin)"
    assert p._dropped_trigger_count == 0


def test_three_triggers_preserve_order(pipeline_instance):
    """2) Uchta trigger — tartib (FIFO) saqlanadi."""
    p = pipeline_instance
    p.plc_on()   # trigger_sequence=1 -> darhol boshlanadi
    p.plc_on()   # trigger_sequence=2 -> navbatga
    p.plc_on()   # trigger_sequence=3 -> navbatga

    assert p._trigger_sequence == 3
    queued_sequences = [seq for seq, _ in p._pending_triggers]
    assert queued_sequences == [2, 3], "navbat FIFO tartibida bo'lishi kerak"


def test_queue_overflow_not_silent_critical_log_and_metric(pipeline_instance, monkeypatch, caplog):
    """3) Navbat to'lganda — jim tashlanmaydi: CRITICAL log + dropped_trigger_count metrikasi."""
    p = pipeline_instance
    monkeypatch.setattr(cfg.SESSION, "max_pending_triggers", 2)

    p.plc_on()          # trigger 1 -> boshlanadi
    p.plc_on()          # trigger 2 -> navbatga (1/2)
    p.plc_on()          # trigger 3 -> navbatga (2/2, navbat TO'LDI)

    assert len(p._pending_triggers) == 2
    assert p._dropped_trigger_count == 0

    caplog.set_level(logging.CRITICAL)
    p.plc_on()          # trigger 4 -> navbat TO'LGAN -> RAD ETILISHI kerak (overflow)

    assert len(p._pending_triggers) == 2, "navbat hajmidan oshmasligi kerak"
    assert p._dropped_trigger_count == 1, "dropped_trigger_count metrikasi oshishi kerak"
    assert any("TRIGGER QUEUE OVERFLOW" in rec.message for rec in caplog.records
               if rec.levelno == logging.CRITICAL), \
        "overflow CRITICAL darajada loglanishi kerak (silent drop EMAS)"

    st = p.status()
    assert st["session"]["dropped_trigger_count"] == 1
    assert st["session"]["pending_triggers"] == 2


def test_queued_trigger_autostarts_after_previous_session_finishes(pipeline_instance):
    """15) Navbatdagi trigger — oldingi sessiya tugagach avtomatik (tartibda) boshlanadi."""
    p = pipeline_instance
    p.plc_on()                       # session A (trigger_sequence=1) boshlanadi
    session_a_id = p._active_session_id
    p.plc_on()                       # trigger 2 -> navbatga
    p.plc_on()                       # trigger 3 -> navbatga
    assert len(p._pending_triggers) == 2

    # Session A ni VIN orqali yakunlaymiz (RFID.enabled=False, shuning uchun
    # faqat VIN kifoya -> SUCCESS bilan finalize bo'ladi).
    p._on_ocr_result(make_ocr_result(session_a_id, vin="NSTFA814ATJ123456"))

    assert wait_until(lambda: p._active_session_id is not None and p._active_session_id != session_a_id,
                      timeout=3.0), "navbatdagi trigger avtomatik boshlanishi kerak"

    new_active = p._active_session_id
    assert new_active != session_a_id
    # Session A endi terminal holatda, tarixda saqlanadi.
    assert p._sessions[session_a_id].state.value == "SUCCESS"
    # Yangi boshlangan sessiya — navbatdagi BIRINCHI (trigger_sequence=2), FIFO tartib.
    assert p._sessions[new_active].trigger_sequence == 2
    # Faqat bitta trigger navbatdan chiqdi — qolgani (trigger_sequence=3) hali navbatda.
    assert len(p._pending_triggers) == 1
    assert p._pending_triggers[0][0] == 3
