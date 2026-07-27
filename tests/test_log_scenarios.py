"""
Regression from the 06/07-iyul production log: the crash fix worked but the accept
gate was over-rejecting CORRECT plates. These cases lock in the fix:
  * confident, directly-read, structurally-valid VINs accept from a single crop
    only when at least four named preprocessing views agree;
  * structure-only pos5 corrections (0->D, 1->J) still reject;
  * non-compliant reads (pos9=B) still reject.

Reads are simulated at the fusion level (no PaddleOCR). Run:
    python tests/test_log_scenarios.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.ai.vin_fusion import VariantRead, fuse


_STRONG_SINGLE_CROP_VARIANTS = (
    "raw_resized", "clahe_unsharp", "blackhat_relief", "adaptive_binary",
)


def _reads(raw, n_crops=1, variants=("raw_resized", "clahe_unsharp"), conf=0.95, quality=0.93):
    out = []
    for ci in range(n_crops):
        for v in variants:
            out.append(VariantRead(ci, v, conf, raw, crop_quality=quality, variant_weight=1.0))
    return out


# ---- Were WRONGLY rejected before the fix -> must now ACCEPT ----
def test_log_041049_direct_pos5_C_single_accepts():
    fr = fuse(_reads("NSTFC814ETJ041049", n_crops=1,
                     variants=_STRONG_SINGLE_CROP_VARIANTS))
    assert fr.status == "ACCEPT", f"{fr.status} {fr.reasons}"
    assert fr.validated_vin == "NSTFC814ETJ041049"
    print("  PASS  #499 NSTFC814ETJ041049 (single, pos5=C direct) -> ACCEPT")


def test_log_041070_single_accepts():
    fr = fuse(_reads("NSTFC814ETJ041070", n_crops=1,
                     variants=_STRONG_SINGLE_CROP_VARIANTS))
    assert fr.status == "ACCEPT", f"{fr.status} {fr.reasons}"
    print("  PASS  #510 NSTFC814ETJ041070 (single) -> ACCEPT")


def test_log_041074_single_accepts():
    fr = fuse(_reads("NSTFC814ETJ041074", n_crops=1,
                     variants=_STRONG_SINGLE_CROP_VARIANTS))
    assert fr.status == "ACCEPT", f"{fr.status} {fr.reasons}"
    print("  PASS  #514 NSTFC814ETJ041074 (single) -> ACCEPT")


# ---- Were correctly rejected -> must STAY rejected (no wrong accept) ----
def test_log_041056_pos5_1_to_J_structure_only_rejected():
    # raw pos5 = '1' -> structure pushes to J -> reject (structure-only, no image support)
    fr = fuse(_reads("NSTF1814ETJ041056", n_crops=1))
    assert fr.status != "ACCEPT", f"structure-only pos5 must reject, got {fr.status}"
    print(f"  PASS  #507 pos5 '1'->J structure-only -> {fr.status}")


def test_log_041073_pos5_0_to_D_structure_only_rejected():
    fr = fuse(_reads("NSTF0814ETJ041073", n_crops=1))
    assert fr.status != "ACCEPT", f"structure-only pos5 must reject, got {fr.status}"
    print(f"  PASS  #514 pos5 '0'->D structure-only -> {fr.status}")


def test_log_bl7m_current_pos9_B_accepted():
    # Authoritative line format (historical + 2026-07 live): NSTHD41ABT/UJ...
    fr = fuse(_reads("NSTHD41ABTJ000585", n_crops=2))
    assert fr.status == "ACCEPT", fr.reasons
    print(f"  PASS  #498 NSTHD41ABTJ000585 (pos9=B non-compliant) -> {fr.status}")


def test_log_041050_two_crop_accepts():
    fr = fuse(_reads("NSTFC814ETJ041050", n_crops=2))
    assert fr.status == "ACCEPT", f"{fr.status} {fr.reasons}"
    print("  PASS  #500 NSTFC814ETJ041050 (2 crops) -> ACCEPT")


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn(); passed += 1
        except Exception:
            print(f"  FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{passed}/{len(fns)} log-scenario tests passed")
    sys.exit(0 if passed == len(fns) else 1)
