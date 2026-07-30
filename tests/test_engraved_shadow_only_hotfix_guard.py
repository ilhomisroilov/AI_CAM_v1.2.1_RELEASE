"""
============================================================
test_engraved_shadow_only_hotfix_guard.py
============================================================
Locks the hard grounded fact: "ENGRAVED_V121 must stay SHADOW_ONLY". This
file documents (with executable proof, not just a docstring) the EXACT
mechanism that keeps it that way today, so the hotfix cannot accidentally
disable it while wiring in dedup/fusion/pos10-verifier changes.

Empirical finding (2026-07-29, verified against the current codebase with a
throwaway sqlite DB -- see WORKER_3_RELEASE/STATUS.md "Integration surface"
for the full transcript):

  * config/settings.yaml currently sets `ocr_release.release_mode:
    GUARDED_PRIMARY` (NOT the dataclass default `SHADOW` in
    backend/config.py's OCRReleaseConfig). This is a real, committed
    production config value (git blame: commit 3b799ae6, 2026-07-28).
  * Despite that, the ORCHESTRATOR's GUARDED_PRIMARY decision has ZERO live
    effect on any persisted field today, because of TWO independent
    safeguards that must BOTH keep holding:
      1. `backend/ai/ocr_shadow_hook.py::dispatch_shadow_ocr_input` (the only
         live call site that runs the engines) calls
         `ShadowOcrStage.process(..., write_final=False)` -- so
         `odb.set_final_ocr` is never reached from that path, no matter what
         `release_mode` says.
      2. `backend/ai/ocr_shadow_hook.py::record_legacy_shadow_final` (called
         from `backend/pipeline.py::_on_ocr_result`, the ONLY call site that
         writes `vin_records.final_ocr_*`) HARDCODES
         `engine=ROLE_PADDLE_RAW`, `decision_reason=SHADOW_ONLY` and the
         LEGACY vin/confidence -- it does not consult the orchestrator's
         result or `OCR_RELEASE.release_mode` AT ALL.
      3. (Separately, `vin_records.detected_vin` -- the actual production
         VIN -- is set exclusively by `pipeline.py::_on_ocr_result` from the
         LEGACY `ocr_worker.py` + `vin_fusion.py` pipeline; the three-engine
         orchestrator never touches it under any release_mode.)

  If a future change removes write_final=False from the live dispatch path,
  or makes record_legacy_shadow_final consult the orchestrator's decision
  instead of hardcoding SHADOW_ONLY, then release_mode=GUARDED_PRIMARY
  (already sitting in settings.yaml today) would go live immediately --
  with an ENGRAVED_V121 model whose 21-class charset cannot even represent
  G/U/V/W (see test_charset_excludes_g_u_v_w below). These tests fail loudly
  if either safeguard is ever removed.

All tests are expected to PASS today (regression lock protecting an
already-safe, but fragile-by-configuration, invariant).
"""
from __future__ import annotations

import sqlite3

import numpy as np

from backend.ai import ocr_contract as C
from backend.ai.ocr_orchestrator import (
    ROLE_ENGRAVED, ROLE_PADDLE_ENHANCED, ROLE_PADDLE_RAW, SHADOW_ONLY,
)
from backend.ai.ocr_shadow_hook import record_legacy_shadow_final
from backend.ai.ocr_shadow_stage import ShadowOcrStage
from backend.config import OCRReleaseConfig
from backend.database import db, ocr_v121_db as odb


# Entirely synthetic test values; not copied from production evidence.
SYNTHETIC_PLATFORM_PREFIX = "NSTFA814"
SYNTHETIC_YEAR_CODE = "T"
SYNTHETIC_SUFFIX = "J"
SYNTHETIC_PRIMARY_VIN = (
    SYNTHETIC_PLATFORM_PREFIX + "E" + SYNTHETIC_YEAR_CODE
    + SYNTHETIC_SUFFIX + "123456"
)
SYNTHETIC_LEGACY_VIN = (
    SYNTHETIC_PLATFORM_PREFIX + "A" + SYNTHETIC_YEAR_CODE
    + SYNTHETIC_SUFFIX + "654321"
)


class _FakeEngine(C.OCRRecognizer):
    """Deterministic in-process engine standing in for ENGRAVED_V121/Paddle."""

    def __init__(self, engine_id: str, text: str, status: str = C.OK):
        self.engine_id = engine_id
        self.text = text
        self.status = status

    def initialize(self):
        pass

    def recognize(self, request):
        return C.make_result(
            request, engine_id=self.engine_id, engine_version="1.2.1",
            engine_status=self.status, charset_id=C.ALPHANUMERIC_36,
            raw_sequence=self.text, normalized_sequence=self.text if self.status == C.OK else "",
            sequence_confidence=0.95,
        )

    def health(self):
        return C.EngineHealth(self.engine_id, True, "ready")

    def metadata(self):
        return C.EngineMetadata(self.engine_id, "1.2.1", C.ALPHANUMERIC_36)

    def close(self):
        pass


def _make_evidence_db(tmp_path, session_id: str):
    path = tmp_path / "shadow_guard.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE vin_records (id INTEGER PRIMARY KEY, session_id TEXT UNIQUE,
        detected_vin TEXT, timestamp TEXT, confidence REAL)"""
    )
    conn.execute(
        "INSERT INTO vin_records(session_id,detected_vin,timestamp,confidence) VALUES (?,?,?,?)",
        (session_id, "", "t", 0.0),
    )
    conn.commit()
    odb.migrate(conn)
    conn.close()
    return path


def test_charset_excludes_g_u_v_w_documenting_why_a_dedicated_pos10_verifier_is_needed():
    """ENGRAVED_V121's trained 21-class charset is
    '0123456789ABCDEFHJNST'. It already excludes G/V (locked by
    tests/test_engraved_v121_engine.py::test_charset_has_no_g_or_v); this
    test additionally locks that it ALSO excludes U and W -- i.e. the model
    can structurally never emit 3 of the 4 valid pos10 year codes
    {T, U, V, W}. This is why pos10 needs its own dedicated verifier/gate
    rather than relying on ENGRAVED_V121 to resolve it."""
    charset = "0123456789ABCDEFHJNST"
    for ch in ("G", "U", "V", "W"):
        assert ch not in charset, f"{ch} unexpectedly present in ENGRAVED_V121 charset"
    assert "T" in charset, "T (2026 code) is the only pos10 year char the model can emit"


def test_guarded_primary_orchestrator_decision_never_persists_via_live_dispatch_path(tmp_path):
    """The ONLY call site that actually runs the 3 engines against live
    session input (`ocr_shadow_hook._run_input` -> `ShadowOcrStage.process`)
    always passes write_final=False. Even with release_mode explicitly set
    to GUARDED_PRIMARY and an ENGRAVED engine that would win the decision,
    vin_records.final_ocr_* must remain untouched (NULL)."""
    sid = "S-guarded"
    path = _make_evidence_db(tmp_path, sid)
    cfg = OCRReleaseConfig(release_mode="GUARDED_PRIMARY")
    engines = {
        ROLE_ENGRAVED: _FakeEngine(ROLE_ENGRAVED, SYNTHETIC_PRIMARY_VIN),
        ROLE_PADDLE_RAW: _FakeEngine(ROLE_PADDLE_RAW, SYNTHETIC_PRIMARY_VIN),
        ROLE_PADDLE_ENHANCED: _FakeEngine(ROLE_PADDLE_ENHANCED, SYNTHETIC_PRIMARY_VIN),
    }
    stage = ShadowOcrStage(cfg, lambda: sqlite3.connect(path), engines)
    result = stage.process(
        session_id=sid, capture_generation=1, normalized_line=np.zeros((10, 10)),
        legacy_text="", legacy_conf=0.0,
        write_final=False,   # <- exactly what the live dispatch path passes
    )
    assert result["ok"] is True

    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT final_ocr_text, final_ocr_engine, final_ocr_decision_reason "
        "FROM vin_records WHERE session_id=?", (sid,)
    ).fetchone()
    conn.close()
    assert row == (None, None, None), (
        "GUARDED_PRIMARY orchestrator decision leaked into vin_records.final_ocr_* "
        "even though write_final=False was passed -- SHADOW-ONLY safety broken"
    )


def test_record_legacy_shadow_final_ignores_release_mode_and_always_writes_legacy_shadow_only(
    tmp_path, monkeypatch,
):
    """`record_legacy_shadow_final` is the ONLY function that writes
    vin_records.final_ocr_*. It must ALWAYS write the legacy VIN with
    engine=PADDLE_RAW and decision_reason=SHADOW_ONLY -- regardless of
    OCR_RELEASE.release_mode -- because it does not even import/consult the
    orchestrator's decision."""
    sid = "S-legacy-shadow"
    path = _make_evidence_db(tmp_path, sid)
    db.set_db_path(path)
    try:
        record_legacy_shadow_final(
            session_id=sid, legacy_vin=SYNTHETIC_LEGACY_VIN, legacy_conf=0.81)
        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT final_ocr_text, final_ocr_engine, final_ocr_decision_reason, "
            "final_ocr_confidence FROM vin_records WHERE session_id=?", (sid,)
        ).fetchone()
        conn.close()
        assert row == (SYNTHETIC_LEGACY_VIN, ROLE_PADDLE_RAW, SHADOW_ONLY, 0.81)
    finally:
        db.set_db_path(None)


def test_record_legacy_shadow_final_writes_empty_engine_when_legacy_vin_is_empty(tmp_path):
    """A NO_READ legacy result (empty vin) must not fabricate an engine name
    or a non-SHADOW_ONLY reason."""
    sid = "S-no-read"
    path = _make_evidence_db(tmp_path, sid)
    db.set_db_path(path)
    try:
        record_legacy_shadow_final(session_id=sid, legacy_vin="", legacy_conf=0.0)
        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT final_ocr_text, final_ocr_engine, final_ocr_decision_reason "
            "FROM vin_records WHERE session_id=?", (sid,)
        ).fetchone()
        conn.close()
        assert row == ("", "", SHADOW_ONLY)
    finally:
        db.set_db_path(None)
