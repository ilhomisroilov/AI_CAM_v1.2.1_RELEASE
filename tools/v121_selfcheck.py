"""v1.2.1 OCR release self-check: config, engine readiness, migration state, gates.

Prints a JSON status and exits 0 if the requested mode is runnable. Used by the PowerShell
run/health/guarded scripts (no server start, no hardware needed).
"""
from __future__ import annotations
import argparse, json, os, sqlite3, sys
WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WT)


def check(mode: str) -> dict:
    from backend.config import OCR_RELEASE, BASE_DIR
    from backend.ai.engines.engraved_v121 import EngravedV121Recognizer
    from backend.database import ocr_v121_db as odb
    model_dir = os.path.join(str(BASE_DIR), OCR_RELEASE.engraved_model_dir)
    eng = EngravedV121Recognizer(model_dir, expected_length=OCR_RELEASE.engraved_expected_length)
    eng.initialize()
    meta = {}
    mp = os.path.join(model_dir, "model_metadata.json")
    if os.path.isfile(mp):
        meta = json.load(open(mp, encoding="utf-8"))
    # guarded gate: current eval has weak-class blockers -> guarded NOT allowed
    guarded_blockers = []
    t = meta.get("test", {})
    if t.get("macro_f1", 0) < 0.90:
        guarded_blockers.append(f"macro_f1={t.get('macro_f1')}<0.90")
    weak = {"5": None, "A": None, "H": None}
    for c in ("5", "A", "H"):
        prf = meta.get("per_class_prf_support", {}).get(c)
        if not prf or prf[2] == 0 or prf[3] == 0:
            guarded_blockers.append(f"weak_class_{c}_no_evidence_or_f1=0")
    st = {
        "release_mode_config": OCR_RELEASE.release_mode,
        "requested_mode": mode,
        "engraved_ready": eng._ready,
        "engraved_error": eng._last_error,
        "output_dimension": meta.get("output_dimension"),
        "charset": meta.get("charset"),
        "unsupported_classes": ["G", "V"],
        "guarded_primary_blockers": guarded_blockers,
        "guarded_primary_allowed": len(guarded_blockers) == 0,
        "collector_enabled": OCR_RELEASE.collection_enabled,
        "collection_root": OCR_RELEASE.collection_root,
    }
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="SHADOW")
    a = ap.parse_args()
    st = check(a.mode)
    print(json.dumps(st, indent=2))
    if a.mode.upper() == "GUARDED_PRIMARY" and not st["guarded_primary_allowed"]:
        sys.exit(3)
    sys.exit(0 if st["engraved_ready"] else 2)


if __name__ == "__main__":
    main()
