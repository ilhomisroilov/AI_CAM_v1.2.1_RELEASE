"""Tests for the Engraved OCR v1.2.1 engine adapter (real trained model)."""
from __future__ import annotations
import os, glob, json, copy
import numpy as np
import pytest
from PIL import Image

from backend.ai import ocr_contract as C
from backend.ai.engines.engraved_v121 import EngravedV121Recognizer, UNKNOWN_CHAR

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELD = os.path.join(WT, "models", "engraved_ocr_v1.2.1")
LAB = os.environ.get("AI_CAM_OCR_DATASET_ROOT",
                     os.path.join(WT, "external_ocr_dataset"))
NORM = os.path.join(LAB, "line_dataset", "images_normalized")

pytestmark = pytest.mark.skipif(not os.path.isfile(os.path.join(MODELD, "model.onnx")),
                                reason="engraved v1.2.1 model artifacts not present")

def _line_image():
    files = sorted(glob.glob(os.path.join(NORM, "*.png")))
    return np.asarray(Image.open(files[0]).convert("L")) if files else np.zeros((64, 1088), np.uint8)

def _request(img, expected_len=17):
    return C.OCRRecognitionRequest(session_id="S1", capture_generation=1, job_id=1,
                                   crops=[C.CropRef(crop_index=0, image=img)], submitted_at=1.0)

def test_engine_initializes_and_validates_contract():
    eng = EngravedV121Recognizer(MODELD)
    eng.initialize()
    assert eng._ready, f"engine failed to init: {eng._last_error}"
    m = eng.metadata()
    assert m.engine_id == "ENGRAVED_V121"
    meta = json.load(open(os.path.join(MODELD, "model_metadata.json")))
    assert meta["output_dimension"] == 22 and meta["charset_len"] == 21

def test_charset_has_no_g_or_v():
    charset = open(os.path.join(MODELD, "charset.txt")).read().strip()
    assert charset == "0123456789ABCDEFHJNST"
    assert "G" not in charset and "V" not in charset  # structurally impossible outputs

def test_recognize_returns_structured_result_and_never_invents():
    eng = EngravedV121Recognizer(MODELD)
    eng.initialize()
    res = eng.recognize(_request(_line_image()))
    assert res.session_id == "S1" and res.capture_generation == 1 and res.job_id == 1
    assert res.char_predictions is not None and len(res.char_predictions) == 17
    # raw is per-position argmax; gated positions that fail the gate are UNKNOWN, not guessed
    assert len(res.raw_sequence) == 17
    # a gated-out position must be UNKNOWN in gated (not a fabricated char); normalized empty if any unknown
    if UNKNOWN_CHAR in res.raw_sequence or res.engine_status != C.OK:
        assert res.normalized_sequence == "" or UNKNOWN_CHAR not in res.normalized_sequence
    # every emitted char is within the enabled charset (never G/V/other)
    charset = open(os.path.join(MODELD, "charset.txt")).read().strip()
    for ch in res.normalized_sequence:
        assert ch in charset

def test_engine_refuses_start_on_contract_mismatch(tmp_path):
    # copy artifacts, corrupt metadata output_dimension -> engine must not become ready
    import shutil
    d = tmp_path / "m"; d.mkdir()
    for f in ("model.onnx", "charset.txt", "checksum.sha256", "confidence_thresholds.json"):
        src = os.path.join(MODELD, f)
        if os.path.isfile(src): shutil.copy2(src, d / f)
    meta = json.load(open(os.path.join(MODELD, "model_metadata.json")))
    meta["output_dimension"] = 99   # wrong
    json.dump(meta, open(d / "model_metadata.json", "w"))
    eng = EngravedV121Recognizer(str(d)); eng.initialize()
    assert not eng._ready
    assert "output_dimension" in (eng._last_error or "")
    # and recognize returns a structured ENGINE_UNAVAILABLE (not a crash, not a fake VIN)
    res = eng.recognize(_request(_line_image()))
    assert res.engine_status == C.ENGINE_UNAVAILABLE and res.normalized_sequence == ""

def test_missing_files_refuses_start(tmp_path):
    eng = EngravedV121Recognizer(str(tmp_path)); eng.initialize()
    assert not eng._ready
