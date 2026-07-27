"""Offline replay of the v1.2.1 shadow OCR integration (no PLC/camera/RFID needed).

Runs the real three-engine stage on real stored normalized lines and verifies:
three separate evidence rows, ONE final result, raw-Paddle preservation, collector
triggers + cropchar files, old/new session isolation, and late-result isolation.
Engraved is the real trained model; the Paddle RAW/ENHANCED adapters run their real code
path driven by a deterministic in-process reader (so replay is fast + offline). Exit 0
only if all invariants hold.
"""
from __future__ import annotations
import csv, os, sqlite3, sys, tempfile, ntpath
import numpy as np
from PIL import Image

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WT)
LAB = os.environ.get("AI_CAM_OCR_DATASET_ROOT",
                     os.path.join(WT, "external_ocr_dataset"))
NORM = os.path.join(LAB, "line_dataset", "images_normalized")
MODELD = os.path.join(WT, "models", "engraved_ocr_v1.2.1")

from backend.config import OCRReleaseConfig
from backend.ai.engines.engraved_v121 import EngravedV121Recognizer
from backend.ai.engines.paddle_profiles import PaddleRawRecognizer, PaddleEnhancedRecognizer
from backend.ai.ocr_collector import ActiveLearningCollector
from backend.ai.ocr_shadow_stage import ShadowOcrStage, ocr_v121_health
from backend.ai.ocr_orchestrator import ROLE_ENGRAVED, ROLE_PADDLE_RAW, ROLE_PADDLE_ENHANCED
from backend.database import ocr_v121_db as odb


class _Reader:
    def __init__(self, text): self.text = text
    def read(self, img):
        return [([[0, 0], [10, 0], [10, 10], [0, 10]], self.text, 0.88)]


def _pick_lines(n=2):
    rev = list(csv.DictReader(open(os.path.join(LAB, "review", "review_manifest.csv"),
                                   newline='', encoding='utf-8')))
    seg = {r["source_line_id"]: r for r in csv.DictReader(
        open(os.path.join(LAB, "character_dataset", "manifests", "extraction_candidates.csv"),
             newline='', encoding='utf-8'))}
    out = []
    for r in rev:
        if r.get("line_decision") != "accepted":
            continue
        sc = seg.get(r["source_id"])
        if not sc:
            continue
        nb = ntpath.basename(sc.get("normalized_image_path", ""))
        p = os.path.join(NORM, nb)
        if os.path.isfile(p) and len(r.get("verified_label", "")) == 17:
            out.append((r["source_id"], r["verified_label"], p))
        if len(out) >= n:
            break
    return out


def main() -> int:
    lines = _pick_lines(2)
    if len(lines) < 2:
        print("REPLAY FAILED: not enough stored lines"); return 2
    dbp = os.path.join(tempfile.mkdtemp(), "replay.db")
    conn = sqlite3.connect(dbp)
    conn.execute("""CREATE TABLE vin_records (id INTEGER PRIMARY KEY, session_id TEXT UNIQUE,
                    detected_vin TEXT, timestamp TEXT, confidence REAL)""")
    for sid, vin, _ in lines:
        conn.execute("INSERT INTO vin_records (session_id,detected_vin,timestamp,confidence) VALUES (?,?,?,?)",
                     (sid, vin, "t", 0.9))
    conn.commit(); odb.migrate(conn); conn.close()
    conn_factory = lambda: sqlite3.connect(dbp)

    eng = EngravedV121Recognizer(MODELD); eng.initialize()
    praw = PaddleRawRecognizer(); praw.attach_test_engine(_Reader(lines[0][1]))
    penh = PaddleEnhancedRecognizer(); penh.attach_test_engine(_Reader(lines[0][1][:-1] + "9"))  # 1-char diff
    engines = {ROLE_ENGRAVED: eng, ROLE_PADDLE_RAW: praw, ROLE_PADDLE_ENHANCED: penh}

    coldir = tempfile.mkdtemp()
    col = ActiveLearningCollector(coldir, store_fn=lambda i: odb.store_collection_item(conn_factory(), i),
                                  enabled=True)
    cfg = OCRReleaseConfig(release_mode="SHADOW", collection_root=coldir)
    stage = ShadowOcrStage(cfg, conn_factory, engines, collector=col)

    checks = {}
    # session A (current owner)
    sidA, vinA, pA = lines[0]
    imgA = np.asarray(Image.open(pA).convert("L"))
    outA = stage.process(session_id=sidA, capture_generation=1, normalized_line=imgA, frame=imgA,
                         trusted_vin=vinA, legacy_text=vinA, legacy_conf=0.9, is_owned=lambda s: True)
    c = conn_factory(); evA = odb.get_engine_results(c, sidA)
    finA = c.execute("SELECT final_ocr_text, final_ocr_decision_reason FROM vin_records WHERE session_id=?",
                     (sidA,)).fetchone(); c.close()
    checks["three_evidence_rows"] = len(evA) == 3
    checks["one_final_result"] = finA is not None and finA[0] == vinA
    checks["shadow_final_is_legacy"] = finA[1] == "SHADOW_ONLY"
    raw_row = [r for r in evA if r["engine_name"] == ROLE_PADDLE_RAW]
    checks["raw_paddle_preserved"] = bool(raw_row) and raw_row[0]["raw_text"] == vinA
    checks["disagreement_detected"] = bool(outA.get("disagreement"))

    # late/old session isolation: sidB (old) result must not write to sidA (new/current)
    sidB, vinB, pB = lines[1]
    imgB = np.asarray(Image.open(pB).convert("L"))
    stage.process(session_id=sidB, capture_generation=0, normalized_line=imgB, frame=imgB,
                  trusted_vin=vinB, legacy_text=vinB, legacy_conf=0.9,
                  is_owned=lambda s: s == sidA)   # only sidA currently owns capture
    c = conn_factory()
    evB = odb.get_engine_results(c, sidB)
    finB = c.execute("SELECT final_ocr_text FROM vin_records WHERE session_id=?", (sidB,)).fetchone()
    finA2 = c.execute("SELECT final_ocr_text FROM vin_records WHERE session_id=?", (sidA,)).fetchone(); c.close()
    checks["late_evidence_stored_under_old_session"] = len(evB) == 3
    checks["late_did_not_overwrite_new_session_final"] = finA2[0] == vinA
    checks["late_no_final_for_unowned_session"] = (finB is None or not finB[0])

    col.flush(6.0)
    crop_files = [f for _, _, fs in os.walk(coldir) for f in fs if f.startswith("pos_") and f.endswith(".png")]
    checks["cropchar_files_saved"] = len(crop_files) > 0
    checks["collector_nonblocking_no_errors"] = col.errors == 0
    h = ocr_v121_health(cfg, engines, conn_factory, collector=col, stage=stage)
    checks["health_migration_applied"] = h["migration_state"].get("applied", False)
    checks["health_engraved_ready"] = h["engines"].get(ROLE_ENGRAVED, {}).get("ready", False)

    ok = all(checks.values())
    print("=== v1.2.1 OFFLINE REPLAY ===")
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print(f"engines: {list(outA.get('engines', []))} | paddle_mode=in_process_deterministic | "
          f"engraved=real | latency_ms={ {k: round(v,1) for k,v in stage.last_latency_ms.items()} }")
    print(f"crop files: {len(crop_files)} | collector collected={col.collected} errors={col.errors}")
    print("REPLAY " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
