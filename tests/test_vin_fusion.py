"""
Unit tests — position-level fusion + accept gates (vin_fusion.py).
Run:  python tests/test_vin_fusion.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.ai.vin_fusion import VariantRead, fuse

GOOD = "NSTFB814ETJ039378"        # valid QY VIN (pos5=B)


def _reads(vin, n_crops=3, variants=("raw_resized", "clahe_unsharp", "blackhat_relief"),
           conf=0.95, quality=0.9):
    out = []
    for ci in range(n_crops):
        for v in variants:
            out.append(VariantRead(crop_index=ci, variant_name=v, ocr_conf=conf,
                                   raw_text=vin, crop_quality=quality, variant_weight=1.0))
    return out


def test_clean_multi_crop_accepts():
    r = fuse(_reads(GOOD))
    assert r.status == "ACCEPT", f"expected ACCEPT, got {r.status} reasons={r.reasons}"
    assert r.validated_vin == GOOD
    assert r.model == "QY"
    assert r.final_score >= 0.92
    print("  PASS  clean multi-crop consensus -> ACCEPT")


def test_confident_single_read_requires_explicit_legacy_opt_in():
    # Production fail-closed: high confidence alone is not independent evidence.
    r = fuse(_reads(GOOD, n_crops=1, variants=("raw_resized",)))
    assert r.status == "OCR_AMBIGUOUS"
    # Legacy/HIL opt-in remains available when explicitly requested.
    r = fuse(_reads(GOOD, n_crops=1, variants=("raw_resized",)),
             accept_single_read=True)
    assert r.status == "ACCEPT", f"explicit legacy opt-in should accept, got {r.status} {r.reasons}"
    assert r.validated_vin == GOOD
    print("  PASS  single direct VIN fails closed; explicit legacy opt-in works")


def test_bl7m_single_wrong_year_code_does_not_false_accept():
    # 2026-07-22 real replay regression: ground truth ABUJ, Paddle returned the
    # structurally legal future code ABVJ with 0.93 confidence. One read is not
    # enough evidence to release a VIN to production.
    wrong = "NSTHD41ABVJ000764"
    r = fuse(_reads(wrong, n_crops=1, variants=("raw_resized",), conf=0.93))
    assert r.status == "OCR_AMBIGUOUS"
    assert r.validated_vin == wrong


def test_low_confidence_single_not_accepted():
    # A structurally-valid VIN read by ONE low-confidence/low-quality crop must not be
    # accepted solely because it is fully_compliant (final_score below gate).
    r = fuse(_reads(GOOD, n_crops=1, variants=("adaptive_binary",), conf=0.55, quality=0.4))
    assert r.status != "ACCEPT", f"low-confidence single should NOT accept, got {r.status}"
    print("  PASS  fully_compliant alone (low conf) does NOT accept")


def test_pos5_structure_only_rejected():
    # Raw OCR sees '0' at pos5 (a suspect). '0' is not allowed at pos5; structure would
    # push it to 'D' (0<->D confusion). Must be rejected as ambiguous (no image support).
    bad = "NSTF0814ETJ039378"     # pos5 = '0'
    r = fuse(_reads(bad, n_crops=3))
    assert r.status == "OCR_AMBIGUOUS", f"pos5 structure-only should be AMBIGUOUS, got {r.status}"
    assert not r.pos5_audit["passed"]
    print(f"  PASS  pos5 structure-only (0->D) rejected -> {r.status}; "
          f"pos5={r.pos5_audit['raw_direct']}->{r.pos5_audit['chosen']}")


def test_pos5_structure_only_3_to_b_stays_ambiguous_without_exact_evidence():
    # 3/8 -> B ni faqat strukturadan taxmin qilish false accept xavfi. Kamida
    # boshqa exact raw/CLAHE yoki mustaqil crop B ni bevosita ko'rmaguncha reject.
    r = fuse(
        _reads("NSTF3814ETJ041257", n_crops=1, variants=("raw_resized", "clahe_unsharp")),
        accept_min_crops=1,
    )
    assert r.status == "OCR_AMBIGUOUS", r.reasons
    assert r.validated_vin == "NSTFB814ETJ041257"
    assert r.pos5_audit.get("rescued") is not True
    print("  PASS  pos5 structure-only 3->B -> OCR_AMBIGUOUS")


def test_serial_tail_trusted_full_vin_conflict_rejected():
    # Real 2026-07-22 regression: raw read ...2188, CLAHE read true ...2186.
    # Both are structurally valid; weighted plurality must not false-accept one.
    reads = [
        VariantRead(0, "raw_resized", 0.843, "NSTFC814ETJ042188",
                    crop_quality=0.9, variant_weight=1.0),
        VariantRead(0, "clahe_unsharp", 0.777, "NSTFC814ETJ042186",
                    crop_quality=0.9, variant_weight=1.0),
    ]
    r = fuse(reads, accept_min_crops=1, accept_final_score=0.8)
    assert r.status == "OCR_AMBIGUOUS"
    assert r.gate["exact_conflict"]["passed"] is False


def test_invalid_pos5_competitor_does_not_lower_direct_c_margin():
    # C — allowed/direct. T — QY pos5 uchun yaroqsiz va C/D ziddiyati emas.
    reads = [
        VariantRead(0, "raw_resized", 0.94, "NSTFC814ETJ042167"),
        VariantRead(0, "blackhat_relief", 0.90, "NSTFT814ETJ042167"),
    ]
    r = fuse(reads, accept_min_crops=1, accept_final_score=0.8)
    assert r.status == "ACCEPT", r.reasons
    assert r.validated_vin == "NSTFC814ETJ042167"


def test_pos5_structure_only_low_support_rejected():
    # pos5 requires redundancy ONLY when structure-corrected. Raw '0' at pos5 (suspect)
    # from a single crop -> structure-only correction to D -> rejected.
    r = fuse(_reads("NSTF0814ETJ039378", n_crops=1, variants=("raw_resized", "clahe_unsharp")))
    assert r.status != "ACCEPT"
    print("  PASS  pos5 structure-only + low support -> not accepted")


def test_pos5_direct_read_single_crop_multi_variant_accepts():
    # Direct pos5 C may pass from one physical crop only when several named
    # preprocessing views independently return the same full VIN.
    variants = ("raw_resized", "clahe_unsharp", "blackhat_relief", "adaptive_binary")
    r = fuse(_reads("NSTFC814ETJ041049", n_crops=1, variants=variants))
    assert r.status == "ACCEPT", f"direct multi-view pos5=C should accept, got {r.status} {r.reasons}"
    assert r.validated_vin == "NSTFC814ETJ041049"
    print("  PASS  pos5=C directly-read multi-view crop -> ACCEPT")


def test_pos5_conflict_low_margin_ambiguous():
    # Half the reads say pos5=B, half say pos5=C -> margin at pos5 small -> ambiguous.
    b = "NSTFB814ETJ039378"
    c = "NSTFC814ETJ039378"
    reads = _reads(b, n_crops=2) + _reads(c, n_crops=2)
    r = fuse(reads)
    assert r.status == "OCR_AMBIGUOUS", f"conflicting pos5 should be AMBIGUOUS, got {r.status}"
    print(f"  PASS  pos5 B/C conflict low margin -> {r.status} (margin={r.pos5_audit['margin']})")


def test_no_17char_reads_is_no_read():
    reads = [VariantRead(0, "raw_resized", 0.9, "NSTFB814"),
             VariantRead(0, "clahe_unsharp", 0.9, "NSTFB814ETJ03937812345")]
    r = fuse(reads)
    assert r.status == "NO_READ"
    print("  PASS  no 17-char reads -> NO_READ")


def test_wrong_vin_not_accepted_over_structure():
    # pos9='6' (not A/E) — structurally invalid; must never accept.
    bad = "NSTFB8146TJ0393789"[:17]
    r = fuse(_reads("NSTFB8146TJ039378", n_crops=3))
    assert r.status != "ACCEPT"
    print(f"  PASS  structurally-invalid pos9=6 -> {r.status} (not accepted)")


def test_audit_present_on_accept():
    r = fuse(_reads(GOOD))
    assert r.status == "ACCEPT"
    assert len(r.decisions) == 17
    d5 = r.decisions[4]
    assert d5.support_count >= 1 and d5.source_variants
    assert "final_score" in r.gate and "pos5" in r.gate
    assert r.pos5_audit["support_crops"] >= 2
    print("  PASS  accepted VIN carries full audit (consensus, margins, pos5, variants)")


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn(); passed += 1
        except Exception:
            print(f"  FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{passed}/{len(fns)} fusion tests passed")
    sys.exit(0 if passed == len(fns) else 1)
