from __future__ import annotations

import inspect
import sqlite3
import time

import numpy as np

from backend.ai import ocr_contract as C
from backend.ai.engines.paddle_profiles import (
    PaddleEnhancedRecognizer,
    PaddleRawRecognizer,
    paddle_inference_counters,
    reset_paddle_inference_counters,
)
from backend.ai.ocr_orchestrator import (
    OcrOrchestrator,
    ROLE_ENGRAVED,
    ROLE_PADDLE_ENHANCED,
    ROLE_PADDLE_RAW,
)
from backend.ai.ocr_shadow_stage import ShadowOcrStage
from backend.config import OCRReleaseConfig
from backend.database import ocr_v121_db as odb
from backend.pipeline import Pipeline

VIN = "NSTFC814ETJ042250"


class _PaddleSpy:
    def __init__(self):
        self.inputs = []

    def read(self, image):
        self.inputs.append(np.asarray(image).copy())
        box = [[0, 0], [100, 0], [100, 20], [0, 20]]
        return [(box, VIN, 0.97)]


class _EngravedFake(C.OCRRecognizer):
    engine_id = ROLE_ENGRAVED

    def __init__(self, status=C.OK, raw=VIN, warning=""):
        self.status = status
        self.raw = raw
        self.warning = warning

    def initialize(self):
        pass

    def recognize(self, request):
        return C.make_result(
            request,
            engine_id=self.engine_id,
            engine_version="1.2.1",
            engine_status=self.status,
            charset_id=C.ALPHANUMERIC_36,
            raw_sequence=self.raw,
            normalized_sequence=self.raw if self.status == C.OK else "",
            sequence_confidence=0.9,
            raw_payload={"source": "fake", "raw": self.raw},
            boxes=[[[0, 0], [100, 20]]],
            warnings=[self.warning] if self.warning else [],
        )

    def health(self):
        return C.EngineHealth(self.engine_id, True, "ready")

    def metadata(self):
        return C.EngineMetadata(self.engine_id, "1.2.1", C.ALPHANUMERIC_36)

    def close(self):
        pass


class _CollectorSpy:
    def __init__(self):
        self.jobs = []

    def submit(self, job):
        self.jobs.append(job)


def _request(image):
    return C.OCRRecognitionRequest(
        session_id="S",
        capture_generation=7,
        job_id="J",
        crops=[C.CropRef(0, image)],
        submitted_at=time.time(),
    )


def test_raw_and_enhanced_execute_two_real_adapter_invocations():
    raw_spy = _PaddleSpy()
    enhanced_spy = _PaddleSpy()
    raw = PaddleRawRecognizer()
    enhanced = PaddleEnhancedRecognizer()
    raw.attach_test_engine(raw_spy)
    enhanced.attach_test_engine(enhanced_spy)
    reset_paddle_inference_counters()

    gradient = np.tile(np.arange(256, dtype=np.uint8), (64, 2))
    result = OcrOrchestrator(
        {
            ROLE_ENGRAVED: _EngravedFake(),
            ROLE_PADDLE_RAW: raw,
            ROLE_PADDLE_ENHANCED: enhanced,
        },
        timeout_ms=2000,
    ).run(_request(gradient), legacy_result_text=VIN, legacy_conf=0.9)

    assert set(result.engine_results) == {
        ROLE_ENGRAVED,
        ROLE_PADDLE_RAW,
        ROLE_PADDLE_ENHANCED,
    }
    assert len(raw_spy.inputs) == 1
    assert len(enhanced_spy.inputs) == 1
    assert not np.array_equal(raw_spy.inputs[0], enhanced_spy.inputs[0])
    assert paddle_inference_counters() == {
        ROLE_PADDLE_RAW: 1,
        ROLE_PADDLE_ENHANCED: 1,
    }


def _database(tmp_path):
    path = tmp_path / "shadow.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE vin_records (
        id INTEGER PRIMARY KEY, session_id TEXT UNIQUE, detected_vin TEXT,
        timestamp TEXT, confidence REAL)"""
    )
    conn.executemany(
        "INSERT INTO vin_records(session_id,detected_vin,timestamp,confidence) "
        "VALUES (?,?,?,?)",
        [("AMB", "", "t", 0.0), ("NO", "", "t", 0.0)],
    )
    odb.migrate(conn)
    conn.close()
    return path


def test_ambiguous_and_no_read_common_inputs_reach_collector(tmp_path):
    path = _database(tmp_path)
    collector = _CollectorSpy()
    ambiguous = {
        role: _EngravedFake(C.EMPTY, "NSTF?814ETJ042250", "OCR_AMBIGUOUS")
        for role in (ROLE_ENGRAVED, ROLE_PADDLE_RAW, ROLE_PADDLE_ENHANCED)
    }
    no_read = {
        role: _EngravedFake(C.EMPTY, "", "NO_READ")
        for role in (ROLE_ENGRAVED, ROLE_PADDLE_RAW, ROLE_PADDLE_ENHANCED)
    }
    cfg = OCRReleaseConfig(collection_enabled=True)
    image = np.zeros((64, 256), dtype=np.uint8)

    ShadowOcrStage(
        cfg, lambda: sqlite3.connect(path), ambiguous, collector=collector
    ).process(
        session_id="AMB",
        capture_generation=1,
        normalized_line=image,
        write_final=False,
    )
    ShadowOcrStage(
        cfg, lambda: sqlite3.connect(path), no_read, collector=collector
    ).process(
        session_id="NO",
        capture_generation=2,
        normalized_line=image,
        write_final=False,
    )

    assert [job["session_id"] for job in collector.jobs] == ["AMB", "NO"]
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT session_id, COUNT(*) FROM ocr_engine_results "
            "GROUP BY session_id ORDER BY session_id"
        ).fetchall()
        payload = conn.execute(
            "SELECT raw_payload_json, boxes_json FROM ocr_engine_results "
            "WHERE session_id='AMB' ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert rows == [("AMB", 3), ("NO", 3)]
    assert "engine_payload" in payload[0]
    assert payload[1] not in (None, "[]")


def test_live_hook_is_before_legacy_ocr_submission():
    source = inspect.getsource(Pipeline._submit_ocr_frames)
    assert source.index("dispatch_shadow_ocr_input") < source.index(
        "self.ocr.submit_frames"
    )
