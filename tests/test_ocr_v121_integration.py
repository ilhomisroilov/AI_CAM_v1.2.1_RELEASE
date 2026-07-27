"""Offline-verifiable tests for the v1.2.1 shadow OCR integration."""
from __future__ import annotations
import os, sqlite3, time, json
import numpy as np
import pytest

from backend.ai import ocr_contract as C
from backend.ai.ocr_orchestrator import (OcrOrchestrator, ROLE_ENGRAVED, ROLE_PADDLE_RAW,
    ROLE_PADDLE_ENHANCED, SHADOW_ONLY, ENGRAVED_ACCEPTED, ENGRAVED_WEAK_CLASS_BLOCKED)
from backend.ai.ocr_collector import ActiveLearningCollector
from backend.ai.ocr_shadow_stage import ShadowOcrStage, ocr_v121_health
from backend.database import ocr_v121_db as odb
from backend.config import OCRReleaseConfig


# ---- fake engines ----
class FakeEngine(C.OCRRecognizer):
    def __init__(self, engine_id, gated="", raw="", conf=0.95, delay=0.0, crash=False, status=C.OK):
        self.engine_id = engine_id; self.gated = gated; self.raw = raw or gated
        self.conf = conf; self.delay = delay; self.crash = crash; self.status = status
    def initialize(self): pass
    def recognize(self, request):
        if self.delay: time.sleep(self.delay)
        if self.crash: raise RuntimeError("boom")
        preds = [C.OCRCharacterPrediction(position=i, char=(self.gated[i] if i < len(self.gated) else ""),
                 confidence=self.conf, alternatives=[(self.gated[i] if i < len(self.gated) else "?", self.conf),
                                                     ("?", 0.01)]) for i in range(max(len(self.gated), len(self.raw)))]
        return C.make_result(request, engine_id=self.engine_id, engine_version="1", engine_status=self.status,
                             charset_id="x", raw_sequence=self.raw, normalized_sequence=self.gated,
                             sequence_confidence=self.conf, char_predictions=preds,
                             engine_model_id=self.engine_id, engine_model_version="1")
    def health(self): return C.EngineHealth(self.engine_id, True, "ready")
    def metadata(self): return C.EngineMetadata(self.engine_id, "1", "x")
    def close(self): pass

VIN = "NSTFC814ETJ042250"

def _engines(**kw):
    return {ROLE_ENGRAVED: FakeEngine(ROLE_ENGRAVED, gated=kw.get("eng", VIN), raw=kw.get("eng_raw", VIN)),
            ROLE_PADDLE_RAW: FakeEngine(ROLE_PADDLE_RAW, gated=kw.get("raw", VIN)),
            ROLE_PADDLE_ENHANCED: FakeEngine(ROLE_PADDLE_ENHANCED, gated=kw.get("enh", VIN))}

def _req(sid="S1", gen=1):
    img = np.zeros((64, 1088), np.uint8)
    return C.OCRRecognitionRequest(session_id=sid, capture_generation=gen, job_id=sid,
                                   crops=[C.CropRef(0, img)], submitted_at=1.0)


# ---------- orchestrator ----------
def test_three_engines_run_concurrently():
    eng = {r: FakeEngine(r, gated=VIN, delay=0.2) for r in (ROLE_ENGRAVED, ROLE_PADDLE_RAW, ROLE_PADDLE_ENHANCED)}
    o = OcrOrchestrator(eng, timeout_ms=2000)
    t = time.perf_counter(); res = o.run(_req()); dt = time.perf_counter() - t
    assert len(res.engine_results) == 3
    assert dt < 0.5, f"engines not concurrent (took {dt:.2f}s for 3x0.2s)"

def test_one_engine_timeout_does_not_cancel_others():
    eng = {ROLE_ENGRAVED: FakeEngine(ROLE_ENGRAVED, gated=VIN, delay=1.5),
           ROLE_PADDLE_RAW: FakeEngine(ROLE_PADDLE_RAW, gated=VIN),
           ROLE_PADDLE_ENHANCED: FakeEngine(ROLE_PADDLE_ENHANCED, gated=VIN)}
    res = OcrOrchestrator(eng, timeout_ms=400).run(_req())
    assert ROLE_ENGRAVED in res.timeouts
    assert ROLE_PADDLE_RAW in res.engine_results and ROLE_PADDLE_ENHANCED in res.engine_results

def test_one_engine_exception_isolated():
    eng = _engines(); eng[ROLE_ENGRAVED] = FakeEngine(ROLE_ENGRAVED, crash=True)
    res = OcrOrchestrator(eng, timeout_ms=2000).run(_req())
    assert ROLE_ENGRAVED in res.failures
    assert ROLE_PADDLE_RAW in res.engine_results and ROLE_PADDLE_ENHANCED in res.engine_results

def test_shadow_final_is_legacy_not_engraved():
    res = OcrOrchestrator(_engines(eng="WRONGENGRAVED0001"), release_mode="SHADOW").run(
        _req(), legacy_result_text=VIN, legacy_conf=0.9)
    assert res.decision_reason == SHADOW_ONLY and res.final_text == VIN

def test_guarded_accepts_clean_engraved():
    # weak-free VIN (no 5/6/7/A/B/D/H) so the weak-class gate does not block it
    clean = "NSTFC814ETJ042230"
    res = OcrOrchestrator(_engines(eng=clean, raw=clean, enh=clean),
                          release_mode="GUARDED_PRIMARY").run(_req(), legacy_result_text=clean)
    assert res.decision_reason == ENGRAVED_ACCEPTED and res.final_text == clean and res.final_engine == ROLE_ENGRAVED

def test_guarded_blocks_weak_class_by_default():
    # engraved gated contains weak class 'D' -> blocked when weak disabled
    res = OcrOrchestrator(_engines(eng="NSTHD41ABTJ000585"), release_mode="GUARDED_PRIMARY",
                          weak_enabled=False).run(_req(), legacy_result_text=VIN)
    assert "ENGRAVED_WEAK_CLASS_BLOCKED" in res.decision_reason

def test_session_ownership_preserved_and_late_isolated():
    res = OcrOrchestrator(_engines(), release_mode="SHADOW").run(_req("SNEW", 5), legacy_result_text=VIN)
    for r in res.engine_results.values():
        assert r.session_id == "SNEW" and r.capture_generation == 5


# ---------- DB migration ----------
def _tmpdb(tmp_path):
    p = str(tmp_path / "t.db")
    conn = sqlite3.connect(p)
    conn.execute("""CREATE TABLE vin_records (id INTEGER PRIMARY KEY, session_id TEXT UNIQUE,
                    detected_vin TEXT, timestamp TEXT, confidence REAL)""")
    conn.execute("INSERT INTO vin_records (session_id, detected_vin, timestamp, confidence) VALUES ('S1','OLD','t',1.0)")
    conn.commit()
    return p, conn

def test_migration_additive_idempotent_and_old_records(tmp_path):
    p, conn = _tmpdb(tmp_path)
    st1 = odb.migrate(conn); st2 = odb.migrate(conn)   # idempotent
    assert st1["applied"] and st2["applied"]
    assert st2["engine_table"] and st2["collection_table"] and st2["final_ocr_columns"]
    old = conn.execute("SELECT detected_vin FROM vin_records WHERE session_id='S1'").fetchone()
    assert old[0] == "OLD"   # old record readable + unchanged

def test_engine_evidence_and_final_and_json_roundtrip(tmp_path):
    p, conn = _tmpdb(tmp_path); odb.migrate(conn)
    odb.store_engine_result(conn, session_id="S1", engine_name=ROLE_PADDLE_RAW, raw_text="NSTF..",
                            per_character=[{"position":0,"char":"N"}], boxes=[{"x":1}], status="OK")
    rows = odb.get_engine_results(conn, "S1")
    assert len(rows) == 1 and json.loads(rows[0]["per_character_json"])[0]["char"] == "N"
    odb.set_final_ocr(conn, session_id="S1", text=VIN, engine=ROLE_PADDLE_RAW, confidence=0.9,
                      decision_reason=SHADOW_ONLY, model_version="1.2.1", disagreement=False)
    r = conn.execute("SELECT final_ocr_text, final_ocr_decision_reason FROM vin_records WHERE session_id='S1'").fetchone()
    assert r[0] == VIN and r[1] == SHADOW_ONLY


# ---------- shadow stage: one final result + evidence ----------
def test_shadow_stage_stores_three_evidence_one_final(tmp_path):
    p, conn = _tmpdb(tmp_path); odb.migrate(conn); conn.close()
    cfg = OCRReleaseConfig(release_mode="SHADOW", collection_enabled=False)
    stage = ShadowOcrStage(cfg, lambda: sqlite3.connect(p), _engines(eng="WRONG0000000000001"))
    out = stage.process(session_id="S1", capture_generation=1, normalized_line=np.zeros((64,1088),np.uint8),
                        trusted_vin=VIN, legacy_text=VIN, legacy_conf=0.9, is_owned=lambda s: True)
    assert out["ok"] and out["decision_reason"] == SHADOW_ONLY
    c = sqlite3.connect(p)
    ev = odb.get_engine_results(c, "S1"); fin = c.execute(
        "SELECT final_ocr_text, final_ocr_engine FROM vin_records WHERE session_id='S1'").fetchone()
    c.close()
    assert len(ev) == 3                                   # three evidence rows
    assert fin[0] == VIN                                  # ONE final = legacy (shadow didn't alter it)


# ---------- collector ----------
def test_collector_saves_crop_and_is_dedup_and_nonblocking(tmp_path):
    seen = set()
    def store(item):
        k = (item["source_hash"], item["character_position"], item["crop_hash"], item["session_id"])
        if k in seen: return False
        seen.add(k); return True
    col = ActiveLearningCollector(str(tmp_path / "col"), store_fn=store, enabled=True)
    line = (np.random.rand(64, 1088) * 255).astype(np.uint8)
    job = {"session_id": "S1", "expected_vin": "NSTFG814EVJ042250", "trusted_label": True,
           "normalized_line": line, "frame": None,
           "per_char": [{"position": 5, "char": "", "conf": 0.2, "reasons": ["UNKNOWN"]},   # pos6 -> G expected (unsupported+target)
                        {"position": 9, "char": "V", "conf": 0.4, "reasons": ["LOW_CONFIDENCE"]}]}
    t = time.perf_counter(); col.submit(job); assert (time.perf_counter()-t) < 0.05   # non-blocking
    col.flush(5.0)
    files = [f for _,_,fs in os.walk(str(tmp_path/"col")) for f in fs if f.endswith(".png")]
    assert any(f.startswith("pos_") for f in files), "no char crop saved"
    assert col.collected >= 1 and col.errors == 0
    n1 = col.collected; col._process(job)                  # dedup on re-run
    assert col.collected == n1

def test_collector_never_renames_unsupported_as_known(tmp_path):
    saved = []
    col = ActiveLearningCollector(str(tmp_path/"c"), store_fn=lambda i: (saved.append(i) or True), enabled=True)
    line = (np.random.rand(64, 1088)*255).astype(np.uint8)
    col._process({"session_id":"S","expected_vin":"NSTFG814EVJ042250","trusted_label":True,
                  "normalized_line":line,"frame":None,
                  "per_char":[{"position":4,"char":"C","conf":0.9,"reasons":["TARGET_CHAR"]}]})  # index4=G expected
    g = [i for i in saved if i["expected_character"] == "G"]
    assert g and g[0]["model_character"] == "UNKNOWN_CHAR"   # G never emitted as a known class

def test_collector_error_is_nonblocking():
    def bad(item): raise RuntimeError("db down")
    col = ActiveLearningCollector("/nonexistent_root_xyz/col", store_fn=bad, enabled=True)
    col.submit({"session_id":"S","normalized_line":np.zeros((64,64),np.uint8),"per_char":[]})
    col.flush(1.0)   # must not raise


# ---------- health + guarded default ----------
def test_health_reports_mode_charset_and_unsupported(tmp_path):
    p, conn = _tmpdb(tmp_path); odb.migrate(conn); conn.close()
    cfg = OCRReleaseConfig()
    h = ocr_v121_health(cfg, _engines(), lambda: sqlite3.connect(p))
    assert h["release_mode"] == "SHADOW"                       # safe default
    assert h["unsupported_classes"] == ["G", "V"]
    assert h["output_dimension"] == 22 and h["migration_state"]["applied"]
    assert h["weak_classes_enabled"] is False                  # guarded weak-classes off by default

def test_guarded_primary_disabled_by_default():
    assert OCRReleaseConfig().release_mode == "SHADOW"


# ---------- collection review queue + import (once only) ----------
def _seed_collection(tmp_path):
    p = str(tmp_path / "c.db"); conn = sqlite3.connect(p)
    conn.execute("""CREATE TABLE vin_records (id INTEGER PRIMARY KEY, session_id TEXT UNIQUE,
                    detected_vin TEXT, timestamp TEXT, confidence REAL)""")
    odb.migrate(conn)
    crop = str(tmp_path / "pos_09_expected_5.png")
    open(crop, "wb").write(b"\x89PNG\r\n")   # placeholder file
    for cid, ch, cp in [("c1", "5", crop), ("c2", "G", crop)]:   # 5 supported; G unsupported
        odb.store_collection_item(conn, {
            "collection_id": cid, "session_id": "S", "expected_vin": "NSTHD41ABTJ000585",
            "expected_character": ch, "character_position": 9, "model_character": "UNKNOWN_CHAR",
            "model_confidence": 0.4, "paddle_raw_character": None, "paddle_enhanced_character": None,
            "collection_reason": "LOW_CONFIDENCE", "source_frame_path": "", "normalized_line_path": "",
            "character_crop_path": cp, "crop_box_json": "{}", "source_hash": "h" + cid, "crop_hash": "k" + cid,
            "label_status": "TRUSTED_LABEL", "review_status": "PENDING", "model_version": "1.2.1",
            "created_at": "t"})
    conn.commit(); conn.close()
    return p

def test_collection_queue_and_import_once(tmp_path):
    import importlib.util
    def load(name, path):
        s = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(s)
        s.loader.exec_module(m); return m
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bq = load("bq", os.path.join(root, "tools", "build_collection_review_queue.py"))
    imp = load("imp", os.path.join(root, "tools", "import_reviewed_collection.py"))
    dbp = _seed_collection(tmp_path)
    qcsv = str(tmp_path / "q.csv")
    n = bq.build(dbp, qcsv)
    assert n == 2   # two PENDING items queued, prioritized
    # human accepts both
    conn = sqlite3.connect(dbp)
    conn.execute("UPDATE ocr_collection_items SET review_status='ACCEPTED'"); conn.commit(); conn.close()
    known = str(tmp_path / "known.csv")
    r1 = imp.import_accepted(dbp, known)
    assert r1["imported"] == 1   # only '5' (supported); 'G' excluded (unsupported)
    r2 = imp.import_accepted(dbp, known)
    assert r2["imported"] == 0   # import cannot happen twice
    conn = sqlite3.connect(dbp)
    st = conn.execute("SELECT review_status FROM ocr_collection_items WHERE collection_id='c1'").fetchone()[0]
    conn.close()
    assert st == "IMPORTED_TO_DATASET"
