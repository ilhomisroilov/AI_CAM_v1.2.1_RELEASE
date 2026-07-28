"""
============================================================
ocr_disagreement.py — F/E · U/V audit + weak-position guard (v1.3.0)
============================================================
Pure helpers (no I/O) for the three-engine OCR evidence layer:

  * char_disagreements(...)  — item 4: per-character disagreement records across
    raw / final / the three engines, flagging the known engraved confusions
    (E/F, U/V, V/J, 0/6, 6/8, 8/0) and validator-only corrections, for dataset
    collection and audit.
  * independent_frame_count(...) — item 2: preprocess variants (CLAHE / sharpen /
    blackhat / rotation) of ONE source frame share a source_frame_hash and count
    as a single independent observation, not several.
  * weak_position_ok(...) — item 5: a high-confidence acceptance at a weak
    position (U/V, F/E) requires >=2 independent frames OR >=2 engine agreement OR
    a high engraved per-character confidence — a lone preprocess variant of one
    frame can never mint a false positive.
"""
from __future__ import annotations

from typing import Dict, List, Optional

# Known engraved-metal confusions (order-independent).
CONFUSABLE_PAIRS = {
    frozenset("EF"), frozenset("UV"), frozenset("VJ"),
    frozenset("06"), frozenset("68"), frozenset("08"),
}
# Characters most prone to confident misreads on etched VINs.
WEAK_POSITIONS = set("UVEF")


def _confusable(a: str, b: str) -> bool:
    return frozenset((a, b)) in CONFUSABLE_PAIRS


def char_disagreements(engine_chars: Dict[str, Optional[str]],
                       raw: Optional[str], final: Optional[str]) -> List[dict]:
    """Per-position disagreement records.

    `engine_chars` maps engine_name -> that engine's gated string (index-aligned
    to `final`). Returns only positions that actually disagree.
    """
    strings = [s for s in list(engine_chars.values()) + [raw, final] if s]
    length = max((len(s) for s in strings), default=0)
    out: List[dict] = []
    for i in range(length):
        rc = raw[i] if raw and i < len(raw) else None
        fc = final[i] if final and i < len(final) else None
        chars = {name: (s[i] if s and i < len(s) else None)
                 for name, s in engine_chars.items()}
        engine_vals = [c for c in chars.values() if c]
        reasons: List[str] = []
        if rc and fc and rc != fc:
            reasons.append("RAW_FINAL_DISAGREEMENT")
            if _confusable(rc, fc):
                reasons.append(f"CONFUSABLE_{rc}_{fc}")
        if len(set(engine_vals)) > 1:
            reasons.append("ENGINE_DISAGREEMENT")
            for a in set(engine_vals):
                for b in set(engine_vals):
                    if a < b and _confusable(a, b):
                        reasons.append(f"CONFUSABLE_{a}_{b}")
        # a character the validator produced that NO engine actually saw
        if fc and engine_vals and fc not in engine_vals:
            reasons.append("VALIDATOR_CHANGED_CHARACTER")
        if reasons:
            out.append({
                "position": i, "raw": rc, "final": fc, "engines": chars,
                "reasons": sorted(set(reasons)),
                "weak_position": (fc in WEAK_POSITIONS) or (rc in WEAK_POSITIONS),
            })
    return out


def independent_frame_count(evidence: List[dict]) -> int:
    """Distinct source frames. Variants sharing a source_frame_hash count once."""
    return len({e.get("source_frame_hash") for e in evidence
                if e.get("source_frame_hash")})


def weak_position_ok(char: Optional[str], *, engine_chars: List[Optional[str]],
                     independent_frames: int, engraved_conf: float = 0.0,
                     min_frames: int = 2, min_agree: int = 2,
                     min_engraved_conf: float = 0.85) -> bool:
    """True if a weak-position character may be accepted with high confidence.

    Non-weak characters are always allowed here (other gates apply). A weak char
    (U/V, F/E) needs independent corroboration.
    """
    if not char or char not in WEAK_POSITIONS:
        return True
    if independent_frames >= min_frames:
        return True
    if sum(1 for c in engine_chars if c == char) >= min_agree:
        return True
    return engraved_conf >= min_engraved_conf
