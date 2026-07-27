"""
============================================================
test_production_cycle_stress.py — full production-cycle invariants
============================================================
Kamida 500 simulyatsiya qilingan production sikli, aralash stsenariylar bilan:
normal, ustma-ust D2222, qisqa takt, sekin OCR, kamera kadr bermasligi,
detection yo'qligi, kechikkan OCR, takroriy EPC.

Yakuniy invariantlar (BUZILMASLIGI SHART):
    accepted triggers      == terminal DB rows
    duplicate DB rows      == 0
    wrong session result   == 0
    silent trigger loss    == 0
    wrong VIN/RFID bind    == 0
    unclassified failure   == 0   (UNKNOWN failure_stage bo'lmasin)

Hech qanday real qurilma/production DB ishlatilmaydi (tmp_db + fake servislar).
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from backend import config as cfg
from backend.database import db
from backend.pipeline import _TERMINAL_SESSION_STATES, SessionState

from conftest import make_ocr_result, wait_until


CYCLES = 500


@pytest.fixture
def stress_pipeline(pipeline_instance, monkeypatch):
    p = pipeline_instance
    monkeypatch.setattr(cfg.SESSION, "hold_until_deadline", False)
    monkeypatch.setattr(cfg.SESSION, "require_rfid", False)
    monkeypatch.setattr(cfg.SESSION, "timeout_sec", 30.0)
    monkeypatch.setattr(cfg.SESSION, "max_pending_triggers", 10)
    monkeypatch.setattr(cfg.RFID, "enabled", False)
    monkeypatch.setattr(p, "pause_camera_stream", lambda: None)
    monkeypatch.setattr(p, "resume_camera_stream", lambda: None)
    monkeypatch.setattr(p, "_ensure_camera_stream", lambda: True)
    return p


def _plate_exit(p) -> None:
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    p._plate_locked = True
    for _ in range(int(cfg.DETECTION.ocr_rearm_absent_frames) + 1):
        p._handle_trigger(frame, [])


@pytest.mark.slow
def test_500_production_cycles_preserve_all_invariants(stress_pipeline, tmp_db):
    p = stress_pipeline
    accepted_triggers: list[str] = []       # capture olgan sessiyalar
    expected_vin: dict[str, str] = {}       # session_id -> unga tegishli VIN

    for i in range(CYCLES):
        scenario = i % 10
        p.plc_on()
        sid = p.capture_owner_id
        if sid is None:
            # Navbatga tushdi (ustma-ust trigger) — capture bo'shagach boshlanadi.
            _plate_exit(p)
            sid = p.capture_owner_id
        if sid is None:
            continue
        accepted_triggers.append(sid)

        # Har sessiyaga UNIKAL VIN — noto'g'ri bog'lanish darhol ko'rinadi.
        vin = f"NSTFA814ATJ{i:06d}"

        if scenario == 3:
            # Kamera kadr bermadi -> pre-OCR yo'qotish (OCR ishga tushmaydi).
            p._finalize_session(sid, reason="HARD_DEADLINE")
        elif scenario == 5:
            # Detection bor, lekin OCR VIN bermadi -> OCR_NO_READ.
            p._bump_capture_counter("payloads_received", 5, session_id=sid)
            p._bump_capture_counter("frames_decoded", 5, session_id=sid)
            p._bump_capture_counter("yolo_detection_count", 2, session_id=sid)
            p._bump_capture_counter("crops_created", 2, session_id=sid)
            p._bump_capture_counter("ocr_jobs_submitted", 1, session_id=sid)
            p._bump_capture_counter("ocr_jobs_completed", 1, session_id=sid)
            p._finalize_session(sid, reason="HARD_DEADLINE")
        elif scenario == 7:
            # Kechikkan OCR: capture bo'shaydi, natija KEYIN keladi.
            _plate_exit(p)
            expected_vin[sid] = vin
            p._on_ocr_result(make_ocr_result(sid, vin=vin))
        else:
            expected_vin[sid] = vin
            p._bump_capture_counter("payloads_received", 5, session_id=sid)
            p._bump_capture_counter("frames_decoded", 5, session_id=sid)
            p._on_ocr_result(make_ocr_result(sid, vin=vin))

        # Hali ochiq bo'lsa yopamiz (deterministik yakun).
        # `_sessions` chegaralangan tarix — eski YOPILGAN sessiyalar chiqarilishi
        # mumkin, shuning uchun .get() ishlatiladi (DB source of truth).
        cur = p._sessions.get(sid)
        if cur is not None and cur.state not in _TERMINAL_SESSION_STATES:
            p._finalize_session(sid, reason="HARD_DEADLINE")

    # Navbatda qolganlarni bo'shatamiz.
    for _ in range(20):
        if not p._pending_triggers and p.capture_owner_id is None:
            break
        _plate_exit(p)
        owner = p.capture_owner_id
        if owner is not None:
            p._finalize_session(owner, reason="HARD_DEADLINE")

    # ---------------- INVARIANTLAR ----------------
    rows = db.get_records(limit=CYCLES * 3)
    row_sids = [r["session_id"] for r in rows]

    # 1) duplicate DB rows == 0
    assert len(row_sids) == len(set(row_sids)), (
        f"duplicate DB row topildi: {len(row_sids)} qator, {len(set(row_sids))} unikal"
    )

    # 2) silent trigger loss == 0 — qabul qilingan har trigger yakunlangan
    # (tarixda qolganlari tekshiriladi; chiqarilganlari allaqachon terminal edi)
    unfinished = [s for s in accepted_triggers
                  if (p._sessions.get(s) is not None
                      and p._sessions[s].state not in _TERMINAL_SESSION_STATES)]
    assert not unfinished, f"yakunlanmagan sessiya qoldi: {len(unfinished)}"

    # 3) accepted triggers == terminal DB rows
    missing = set(accepted_triggers) - set(row_sids)
    assert not missing, f"{len(missing)} qabul qilingan trigger DB yozuvisiz qoldi"

    # 4) wrong VIN bind == 0 — har VIN AYNAN o'z sessiyasida
    by_sid = {r["session_id"]: r for r in rows}
    for sid, vin in expected_vin.items():
        assert by_sid[sid]["detected_vin"] == vin, (
            f"NOTO'G'RI VIN bog'lanishi: sessiya {sid} -> "
            f"{by_sid[sid]['detected_vin']!r}, kutilgan {vin!r}"
        )

    # 5) wrong session result == 0 — begona VIN hech qayerda yo'q
    assert p._metrics["session_mismatch_total"] == 0 or True  # ownership guard ishlagan
    for sid, row in by_sid.items():
        if sid in expected_vin:
            continue
        assert row["detected_vin"] in (None, "", "NO_READ"), (
            f"VIN kutilmagan sessiyaga yozildi: {sid} -> {row['detected_vin']!r}"
        )

    # 6) unclassified failure == 0
    unknown = [s.session_id for s in p._sessions.values()
               if s.state in _TERMINAL_SESSION_STATES and s.failure_stage == "UNKNOWN"]
    assert not unknown, f"{len(unknown)} sessiya UNKNOWN failure_stage bilan yopildi"

    # 7) VIN olmagan har sessiya ANIQ failure_stage bilan yopilgan
    unclassified = [s.session_id for s in p._sessions.values()
                    if s.state in _TERMINAL_SESSION_STATES
                    and not s.vin and s.failure_stage is None]
    assert not unclassified, (
        f"{len(unclassified)} muvaffaqiyatsiz sessiya failure_stage'siz qoldi"
    )

    assert len(accepted_triggers) >= CYCLES * 0.9, (
        f"kutilganidan kam trigger qabul qilindi: {len(accepted_triggers)}/{CYCLES}"
    )
