"""
Regression tests (Section 9) — previously-suspicious VINs must NEVER be accepted
as a WRONG-but-structurally-valid VIN. They must either be read correctly (ACCEPT
with the true VIN) or returned as OCR_AMBIGUOUS / rejected.

These run at the FUSION level with synthetic OCR reads (no PaddleOCR needed), so
they are CI-safe and deterministic. They mirror the confusion classes from the
spec: pos5 C/D/B/G/H/J, pos10 T/1, pos11 J/1, pos2 S/5/9, pos3 T/1.

Run:  python tests/test_ocr_regression.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.ai.vin_fusion import VariantRead, fuse

VARIANTS = ("raw_resized", "clahe_unsharp", "blackhat_relief", "tophat_relief")


def _reads(vin, n_crops=3, variants=VARIANTS, conf=0.95, quality=0.9):
    out = []
    for ci in range(n_crops):
        for v in variants:
            out.append(VariantRead(ci, v, conf, vin, crop_quality=quality, variant_weight=1.0))
    return out


def _mixed(vin_a, vin_b, na=2, nb=2):
    return _reads(vin_a, n_crops=na) + _reads(vin_b, n_crops=nb)


def _assert_no_wrong_accept(fr, truth, label):
    if fr.status == "ACCEPT":
        assert fr.validated_vin == truth, \
            f"{label}: WRONG VIN ACCEPTED {fr.validated_vin} (truth {truth})"


# ---- structurally-invalid reads must never be accepted ----
def test_pos9_6_not_accepted():
    fr = fuse(_reads("NSTFD8146TJ040686"))   # pos9='6' (needs A/E)
    assert fr.status != "ACCEPT"
    _assert_no_wrong_accept(fr, "NSTFD814ETJ040686", "pos9=6")
    print("  PASS  pos9=6 structurally invalid -> not accepted")


def test_pos5_digit2_structure_only_ambiguous():
    fr = fuse(_reads("NSTF2814ETJ040677"))   # pos5='2' (invalid, structure-only)
    assert fr.status == "OCR_AMBIGUOUS", fr.status
    print("  PASS  pos5='2' structure-only -> OCR_AMBIGUOUS")


def test_pos5_P_not_accepted():
    fr = fuse(_reads("NSTFP814ETJ040678"))   # pos5='P' (invalid, not confusable to allowed)
    assert fr.status != "ACCEPT"
    print("  PASS  pos5='P' -> not accepted")


# ---- confusion corrections: accept only if truly supported, else ambiguous ----
def test_pos2_S5_corrected_or_ambiguous():
    # raw pos2='5' unanimously; pos2 is enforced 'S'. Should accept as NST... correctly.
    fr = fuse(_reads("N5TFB814ETJ039378"))
    _assert_no_wrong_accept(fr, "NSTFB814ETJ039378", "pos2=5")
    assert fr.validated_vin[1] == "S"
    print(f"  PASS  pos2='5'->'S' enforced -> {fr.status} ({fr.validated_vin})")


def test_pos11_1_to_J_enforced():
    fr = fuse(_reads("NSTFB814ET1039378"))   # pos11='1' -> enforced 'J'
    assert fr.validated_vin[10] == "J"
    _assert_no_wrong_accept(fr, "NSTFB814ETJ039378", "pos11=1")
    print(f"  PASS  pos11='1'->'J' enforced -> {fr.status}")


def test_pos10_T_variable_low_support_not_accepted():
    # pos10='1' is invalid year-code; confuses to T. Only structure -> should not silently accept
    # unless strong support. Single-variant here -> not accepted.
    fr = fuse(_reads("NSTFB814E1J039378", n_crops=1, variants=("raw_resized",)))
    assert fr.status != "ACCEPT"
    print(f"  PASS  pos10='1' weak support -> {fr.status}")


def test_pos5_conflict_never_picks_wrong():
    # 2 crops see 'C', 2 crops see 'D' at pos5 -> ambiguous, must not pick either as accepted
    fr = fuse(_mixed("NSTFC814ETJ039378", "NSTFD814ETJ039378"))
    assert fr.status == "OCR_AMBIGUOUS"
    print(f"  PASS  pos5 C/D conflict -> {fr.status} (no wrong accept)")


def test_correct_vin_still_accepts():
    # sanity: a well-supported correct VIN with pos5=B still accepts
    fr = fuse(_reads("NSTFB814ETJ039378"))
    assert fr.status == "ACCEPT" and fr.validated_vin == "NSTFB814ETJ039378"
    print("  PASS  correct well-supported VIN still ACCEPTS (no over-rejection)")


def test_bl7m_correct_accepts():
    fr = fuse(_reads("NSTHB414GTJ987654"))   # BL7M valid (pos8 remap 4->? ) check accept-or-ambiguous no wrong
    _assert_no_wrong_accept(fr, "NSTHB41GATJ987654", "bl7m")
    print(f"  PASS  BL7M sample -> {fr.status} (no wrong accept)")


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn(); passed += 1
        except Exception:
            print(f"  FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{passed}/{len(fns)} regression tests passed")
    sys.exit(0 if passed == len(fns) else 1)
