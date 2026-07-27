"""
============================================================
vin_fusion.py  —  Position-level weighted consensus + accept gates
============================================================
Maqsad (Section 4/5): BIRINCHI `fully_compliant` natijani DARHOL qabul qilmaslik.
Barcha crop/filter/burilish OCR natijalarini yig'ib, HAR POZITSIYA bo'yicha
OG'IRLIKLI KO'PCHILIK OVOZI (weighted consensus) bilan yakuniy VIN ni tanlaymiz,
so'ng qat'iy GLOBAL ACCEPT GATE + POSITION-5 HARD GATE dan o'tkazamiz.

MUVOZANAT (jonli log tahlilidan keyin): gate faqat XAVFLI holatlarni rad etadi,
TO'G'RI o'qilgan plastinkalarni EMAS. Xususan:
  * pos5 belgisi TO'G'RIDAN-TO'G'RI o'qilgan va strukturaga mos bo'lsa (margin
    yetarli), bitta crop ham qabul qilinadi — REDUNDANCY faqat STRUKTURA-TUZATISH
    (0->D, 1->J) yoki past margin holatlarida talab qilinadi.
  * enforce qilingan doimiy pozitsiyalar (N,S,T,1,J) "qo'llab-quvvatlanmagan" deb
    hisoblanMAYDI — ular standart, o'ylab topilgan emas.
  * "ishonchli yakka o'qish" yo'li: barcha O'ZGARUVCHAN belgilar to'g'ridan-to'g'ri
    o'qilgan, strukturaga to'liq mos va yuqori ball -> bitta crop yetarli.

Xavfli holatlar odatda rad etiladi: strukturaviy nomuvofiqlik, pos5 struktura-
faqat tuzatish, pos5 past margin (C/D chalkashligi). Jonli liniya uchun faqat
3/8 -> B holati yuqori umumiy ishonch va raw-support bilan rescue qilinadi.

Bu modul backend.config ni IMPORT QILMAYDI — chegaralar parametr sifatida beriladi.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import vin_rules
from .vin_postprocess import CONFUSIONS, PositionDecision, _is_variable_position

# Xavfli pozitsiyalar (0-asosli): pos5(4) C/D/B/G/H/J, pos10(9) T/1, pos11(10) J/1,
# pos2(1) S/5/9, pos3(2) T/1.
RISKY_POSITIONS: Set[int] = {1, 2, 4, 9, 10}
POS5 = 4
SERIAL_POSITIONS: Set[int] = set(range(11, 17))
TRUSTED_VARIANTS = frozenset(("raw_resized", "clahe_unsharp"))
# pos5 da xom OCR ko'rishi mumkin, ammo FAQAT struktura tufayli C/D/B/G ga
# aylantirilsa shubhali belgilar (spec 5-band): 0,3,6,8,R.
POS5_STRUCTURE_ONLY_SUSPECTS = set("0368R")

_ALNUM = re.compile(r"[^A-Z0-9]")


def _norm(s: str) -> str:
    return _ALNUM.sub("", (s or "").upper())


def _confusable(a: str, b: str) -> bool:
    """a va b ma'lum vizual chalkashlik juftimi (ikki tomonlama)."""
    return b in CONFUSIONS.get(a, ()) or a in CONFUSIONS.get(b, ())


@dataclass
class VariantRead:
    """Bitta variant OCR taskining natijasi (fusion kirishi)."""
    crop_index: int
    variant_name: str
    ocr_conf: float
    raw_text: str
    normalized_raw: str = ""
    crop_quality: float = 1.0
    variant_weight: float = 1.0
    # Near-identical frames share one evidence_group. Distinct crop_index alone
    # must not fake independent evidence when the camera repeated a frame.
    evidence_group: Optional[int] = None

    def __post_init__(self):
        if not self.normalized_raw:
            self.normalized_raw = _norm(self.raw_text)
        if self.evidence_group is None:
            self.evidence_group = self.crop_index


@dataclass
class FusionResult:
    status: str                        # ACCEPT | OCR_AMBIGUOUS | NO_READ
    validated_vin: str
    model: Optional[str]
    final_score: float
    compliance: float
    fully_compliant: bool
    raw_support_ratio: float
    n_crops: int
    n_reads: int
    decisions: List[PositionDecision] = field(default_factory=list)
    pos5_audit: dict = field(default_factory=dict)
    gate: dict = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    note: str = ""

    @property
    def accepted(self) -> bool:
        return self.status == "ACCEPT"


class _Vote:
    __slots__ = ("weight", "variants", "crops", "reads")

    def __init__(self):
        self.weight = 0.0
        self.variants: Set[str] = set()
        self.crops: Set[int] = set()
        self.reads = 0

    def add(self, w: float, variant: str, crop: int):
        self.weight += w
        self.variants.add(variant)
        self.crops.add(crop)
        self.reads += 1


def _collect_votes(reads: Sequence[VariantRead]) -> List[Dict[str, _Vote]]:
    """17 pozitsiya uchun char -> _Vote (faqat uzunligi 17 bo'lgan o'qishlar)."""
    votes: List[Dict[str, _Vote]] = [dict() for _ in range(vin_rules.VIN_LENGTH)]
    for r in reads:
        nr = r.normalized_raw
        if len(nr) != vin_rules.VIN_LENGTH:
            continue
        length_score = 1.0
        w = max(0.0, float(r.ocr_conf)) * float(r.variant_weight) \
            * max(0.05, float(r.crop_quality)) * length_score
        if w <= 0:
            w = 1e-6
        for i, ch in enumerate(nr):
            votes[i].setdefault(ch, _Vote()).add(w, r.variant_name, int(r.evidence_group))
    return votes


def _candidate_evidence(vote: Dict[str, _Vote], model: str, pos0: int,
                        confusion_prior: float) -> Tuple[Dict[str, float], str]:
    """
    Pozitsiya uchun EVIDENCE taqsimoti + xom plurallik belgisi.
      * ruxsat etilgan, to'g'ridan-to'g'ri ovoz bergan belgi -> to'liq og'irlik;
      * ruxsat etilmagan belgi -> og'irligini RUXSAT ETILGAN chalkashlik qo'shnisiga
        (confusion_prior bilan) o'tkazadi;
      * ruxsat etilgan belgilar o'zaro smear qilmaydi (fantom raqobat yo'q).
    """
    ev: Dict[str, float] = {}
    raw_direct = max(vote.items(), key=lambda kv: kv[1].weight)[0] if vote else "?"
    for ch, cell in vote.items():
        if vin_rules.is_allowed(model, pos0, ch):
            ev[ch] = ev.get(ch, 0.0) + cell.weight
        else:
            neighbors = [a for a in CONFUSIONS.get(ch, ()) if vin_rules.is_allowed(model, pos0, a)]
            for a in list(vin_rules.allowed_at(model, pos0)):
                if a not in neighbors and _confusable(ch, a):
                    neighbors.append(a)
            if neighbors:
                share = cell.weight * confusion_prior / float(len(neighbors))
                for a in neighbors:
                    ev[a] = ev.get(a, 0.0) + share
            else:
                ev[ch] = ev.get(ch, 0.0) + cell.weight * 0.5
    return ev, raw_direct


def _select_for_model(
    votes: List[Dict[str, _Vote]], model: str, *,
    confusion_prior: float, struct_penalty: float, enforce_fixed_positions: bool,
) -> Tuple[str, List[PositionDecision], float, bool, float]:
    """
    Berilgan model uchun per-position tanlov. Qaytadi:
      (validated_vin, decisions, compliance, fully_compliant, mean_prob)
    """
    chosen: List[str] = []
    decisions: List[PositionDecision] = []
    probs: List[float] = []

    for i in range(vin_rules.VIN_LENGTH):
        vote = votes[i]
        ev, raw_direct = _candidate_evidence(vote, model, i, confusion_prior)
        if not ev:
            ev = {raw_direct if raw_direct != "?" else "?": 1e-6}

        ordered = sorted(ev.items(), key=lambda kv: kv[1], reverse=True)
        best_char = ordered[0][0]

        forced = None
        if enforce_fixed_positions and i in vin_rules.CONSTANT_POSITIONS:
            forced = vin_rules.CONSTANT_POSITIONS[i]
            best_char = forced

        tot = sum(ev.values()) or 1e-9
        prob_best = ev.get(best_char, 0.0) / tot
        second = 0.0
        for c, s in ordered:
            if c != best_char:
                # Strukturaga yaroqsiz va tanlangan belgiga chalkashlik orqali
                # bog'lanmagan belgi top-2 marginni sun'iy pasaytirmasin. Masalan,
                # QY pos5 C ni boshqa variant T deb o'qishi C/D ziddiyati emas.
                if vin_rules.is_allowed(model, i, best_char) and not vin_rules.is_allowed(model, i, c):
                    continue
                second = s / tot
                break
        margin = max(0.0, prob_best - second)
        probs.append(prob_best if best_char != "?" else 0.0)

        cell = vote.get(best_char)
        support_reads = cell.reads if cell else 0
        source_vars = sorted(cell.variants) if cell else []
        directly_seen = best_char in vote
        structure_corrected = (best_char != raw_direct) and (
            (not directly_seen) or (forced is not None and forced != raw_direct))

        chosen.append(best_char)
        decisions.append(PositionDecision(
            pos=i + 1, raw_char=raw_direct, chosen_char=best_char,
            visual=ev.get(best_char, 0.0),
            allowed=(best_char != "?" and vin_rules.is_allowed(model, i, best_char)),
            changed=(best_char != raw_direct),
            support_count=support_reads, margin=float(margin),
            source_variants=source_vars, structure_corrected=bool(structure_corrected),
        ))

    validated = "".join(chosen)
    if len(validated) == vin_rules.VIN_LENGTH and "?" not in validated:
        fully, _flags = vin_rules.validate(model, validated)
        compliance = vin_rules.compliance_fraction(model, validated)
    else:
        fully, compliance = False, 0.0
    mean_prob = sum(probs) / len(probs) if probs else 0.0
    return validated, decisions, compliance, fully, mean_prob


def _weighted_avg_conf(reads: Sequence[VariantRead]) -> float:
    num = den = 0.0
    for r in reads:
        w = max(0.0, float(r.variant_weight)) * max(0.05, float(r.crop_quality))
        num += w * max(0.0, float(r.ocr_conf))
        den += w
    return (num / den) if den > 0 else 0.0


def _mk(passed: bool, value, threshold, extra: str = "") -> dict:
    d = {"passed": bool(passed), "value": value, "threshold": threshold}
    if extra:
        d["extra"] = extra
    return d


def _base_variant(name: str) -> str:
    return str(name or "").split("@", 1)[0]


def _exact_evidence(reads: Sequence[VariantRead], validated: str, model: str,
                    conflict_ratio: float) -> dict:
    """Strict raw full-VIN evidence. Fixed-position correction is not exact evidence."""
    exact = [r for r in reads if r.normalized_raw == validated]
    exact_weight = sum(max(0.0, r.ocr_conf) * max(0.0, r.variant_weight)
                       * max(0.05, r.crop_quality) for r in exact)
    exact_groups = {int(r.evidence_group) for r in exact}
    exact_bases = {_base_variant(r.variant_name) for r in exact}
    trusted_bases = exact_bases & TRUSTED_VARIANTS

    candidates: Dict[str, dict] = {}
    for r in reads:
        raw = r.normalized_raw
        if len(raw) != vin_rules.VIN_LENGTH:
            continue
        raw_model = vin_rules.model_from_vin(raw)
        if raw_model != model or not vin_rules.validate(model, raw)[0]:
            continue
        cell = candidates.setdefault(raw, {"weight": 0.0, "groups": set(), "bases": set()})
        cell["weight"] += (max(0.0, r.ocr_conf) * max(0.0, r.variant_weight)
                           * max(0.05, r.crop_quality))
        cell["groups"].add(int(r.evidence_group))
        cell["bases"].add(_base_variant(r.variant_name))

    competitors = []
    for vin, cell in candidates.items():
        if vin == validated:
            continue
        # A weak/noisy transform is not enough to veto a good result. A competing
        # strict VIN must be supported by a trusted raw/CLAHE family and have
        # material weight relative to the selected exact VIN.
        ratio = cell["weight"] / max(exact_weight, 1e-9)
        if (cell["bases"] & TRUSTED_VARIANTS) and ratio >= conflict_ratio:
            competitors.append({
                "vin": vin, "weight_ratio": round(float(ratio), 3),
                "groups": len(cell["groups"]), "bases": sorted(cell["bases"]),
            })

    return {
        "exact_reads": len(exact),
        "exact_groups": len(exact_groups),
        "exact_bases": sorted(exact_bases),
        "trusted_bases": sorted(trusted_bases),
        "exact_mean_conf": round(sum(r.ocr_conf for r in exact) / len(exact), 3) if exact else 0.0,
        "strict_candidates": sorted(candidates),
        "competing_candidates": competitors,
        "conflict": bool(competitors),
    }


def _pos5_gate(vote: Dict[str, _Vote], decision: PositionDecision, *,
               pos5_min_crops: int, pos5_min_variants: int, pos5_min_margin: float,
               reject_structure_only: bool) -> Tuple[dict, bool, List[str]]:
    """
    Position-5 HARD GATE (Section 5) — muvozanatlangan.

    * Tanlangan belgi strukturaga mos bo'lishi shart (allowed).
    * Top-2 margin >= pos5_min_margin (C/D chalkashligini rad etadi).
    * Agar belgi STRUKTURA-TUZATISH natijasi bo'lsa (xom OCR to'g'ridan-to'g'ri
      ko'rmagan, mas. 0->D, 1->J) -> REDUNDANCY talab qilinadi (>=pos5_min_crops
      crop yoki >=pos5_min_variants variant) VA xom 0/3/6/8/R bo'lsa umuman rad.
    * Agar belgi TO'G'RIDAN-TO'G'RI o'qilgan va strukturaga mos bo'lsa -> bitta
      crop/variant ham yetarli (margin sharti bajarilsa).
    """
    chosen = decision.chosen_char
    cell = vote.get(chosen)
    crops = len(cell.crops) if cell else 0
    variants = len(cell.variants) if cell else 0
    directly_seen = chosen in vote
    raw_direct = max(vote.items(), key=lambda kv: kv[1].weight)[0] if vote else "?"

    reasons: List[str] = []
    ok = True

    if not decision.allowed:
        ok = False
        reasons.append(f"pos5 belgisi strukturaga mos emas ('{chosen}')")

    if decision.margin < pos5_min_margin:
        ok = False
        reasons.append(f"margin past ({decision.margin:.2f}<{pos5_min_margin})")

    structure_only = (not directly_seen) or decision.structure_corrected
    suspect = raw_direct in POS5_STRUCTURE_ONLY_SUSPECTS
    if structure_only:
        support_ok = (crops >= pos5_min_crops) or (variants >= pos5_min_variants)
        if not support_ok:
            ok = False
            reasons.append(f"struktura-tuzatish, support past (crops={crops}<{pos5_min_crops} "
                           f"& variants={variants}<{pos5_min_variants})")
        if reject_structure_only and suspect:
            ok = False
            reasons.append(f"struktura-faqat tuzatish (xom='{raw_direct}' -> '{chosen}', rasm dalili yo'q)")

    audit = {
        "chosen": chosen, "raw_direct": raw_direct,
        "support_crops": crops, "support_variants": variants,
        "margin": round(float(decision.margin), 3),
        "source_variants": decision.source_variants,
        "structure_corrected": bool(structure_only),
        "directly_seen": bool(directly_seen), "passed": bool(ok),
    }
    return audit, ok, reasons


def fuse(
    reads: Sequence[VariantRead],
    *,
    models: Sequence[str] = vin_rules.MODELS,
    confusion_prior: float = 0.6,
    struct_penalty: float = 0.5,
    enforce_fixed_positions: bool = True,
    # global accept gate
    accept_final_score: float = 0.92,
    accept_min_crops: int = 2,
    accept_multi_variant_min: int = 4,
    accept_risky_margin: float = 0.25,
    accept_raw_support_ratio: float = 0.60,
    require_known_model: bool = True,
    # "ishonchli yakka o'qish" yo'li (bitta crop yetarli bo'ladigan holat)
    accept_single_read: bool = False,
    accept_single_min_score: float = 0.92,
    # position-5 hard gate
    pos5_min_crops: int = 2,
    pos5_min_variants: int = 3,
    pos5_min_margin: float = 0.20,
    pos5_reject_structure_only: bool = True,
    pos5_structure_rescue_enabled: bool = True,
    pos5_structure_rescue_raw_chars: str = "38",
    pos5_structure_rescue_min_score: float = 0.90,
    pos5_structure_rescue_min_raw_support: float = 0.85,
    # barcha o'zgaruvchan/serial pozitsiyalar uchun umumiy disagreement gate
    accept_variable_margin: float = 0.08,
    accept_serial_margin: float = 0.12,
    exact_conflict_ratio: float = 0.55,
    exact_rescue_min_conf: float = 0.74,
    strong_consensus_prob: float = 0.80,
    **_ignored,
) -> FusionResult:
    """Barcha variant o'qishlarini birlashtirib yakuniy VIN + accept qarorini beradi."""
    l17 = [r for r in reads if len(r.normalized_raw) == vin_rules.VIN_LENGTH]
    n_reads = len(l17)
    n_crops = len({int(r.evidence_group) for r in l17})

    if n_reads == 0:
        return FusionResult(
            status="NO_READ", validated_vin="", model=None, final_score=0.0,
            compliance=0.0, fully_compliant=False, raw_support_ratio=0.0,
            n_crops=0, n_reads=0, reasons=["17-belgili OCR o'qishi topilmadi"])

    votes = _collect_votes(l17)
    avg_conf = _weighted_avg_conf(l17)

    best = None
    for m in models:
        if not vin_rules.is_supported_model(m):
            continue
        validated, decisions, compliance, fully, mean_prob = _select_for_model(
            votes, m, confusion_prior=confusion_prior, struct_penalty=struct_penalty,
            enforce_fixed_positions=enforce_fixed_positions)
        final_score = 0.5 * compliance + 0.25 * mean_prob + 0.25 * avg_conf
        cand = dict(model=m, validated=validated, decisions=decisions, compliance=compliance,
                    fully=fully, mean_prob=mean_prob, final_score=final_score)
        if best is None or (cand["fully"], cand["final_score"]) > (best["fully"], best["final_score"]):
            best = cand

    if best is None:
        return FusionResult(
            status="OCR_AMBIGUOUS", validated_vin="", model=None, final_score=0.0,
            compliance=0.0, fully_compliant=False, raw_support_ratio=0.0,
            n_crops=n_crops, n_reads=n_reads, reasons=["model qo'llab-quvvatlanmadi"])

    validated = best["validated"]
    decisions = best["decisions"]
    final_score = best["final_score"]
    mean_prob = best["mean_prob"]
    model_final = vin_rules.model_from_vin(validated) or best["model"]
    exact = _exact_evidence(l17, validated, model_final, exact_conflict_ratio) \
        if model_final else {"conflict": False, "exact_reads": 0, "exact_groups": 0,
                             "trusted_bases": [], "competing_candidates": []}

    # enforce qilingan doimiy pozitsiyalar (standart, o'ylab topilgan emas)
    enforced: Set[int] = set(vin_rules.CONSTANT_POSITIONS) if enforce_fixed_positions else set()

    # raw_support_ratio: doimiy pozitsiyalar HAM qo'llab-quvvatlangan deb hisoblanadi
    supported = sum(1 for i, d in enumerate(decisions)
                    if d.support_count > 0 or i in enforced)
    raw_support_ratio = supported / float(vin_rules.VIN_LENGTH)

    # O'ZGARUVCHAN (doimiy bo'lmagan) pozitsiyalarda struktura-tuzatish bormi?
    var_struct_corr = any(d.structure_corrected for i, d in enumerate(decisions)
                          if i not in enforced)

    # xavfli margin (faqat O'ZGARUVCHAN xavfli pozitsiyalar: pos5, pos10)
    risky_margins = [decisions[p].margin for p in RISKY_POSITIONS if p not in enforced]
    min_risky_margin = min(risky_margins) if risky_margins else 1.0
    variable_margins = [d.margin for i, d in enumerate(decisions) if i not in enforced]
    min_variable_margin = min(variable_margins) if variable_margins else 1.0
    serial_margins = [decisions[p].margin for p in SERIAL_POSITIONS]
    min_serial_margin = min(serial_margins) if serial_margins else 1.0

    # POSITION-5 HARD GATE
    pos5_audit, pos5_ok, pos5_reasons = _pos5_gate(
        votes[POS5], decisions[POS5], pos5_min_crops=pos5_min_crops,
        pos5_min_variants=pos5_min_variants, pos5_min_margin=pos5_min_margin,
        reject_structure_only=pos5_reject_structure_only)

    strong_multi_variant = (n_reads >= accept_multi_variant_min and mean_prob >= strong_consensus_prob)
    # ISHONCHLI YAKKA O'QISH: bitta crop, lekin barcha o'zgaruvchan belgilar
    # to'g'ridan-to'g'ri o'qilgan (struktura-tuzatish yo'q), yuqori ball, pos5 toza.
    confident_single = (
        accept_single_read
        and best["fully"]
        and final_score >= accept_single_min_score
        and not var_struct_corr
        and pos5_ok
        and min_risky_margin >= accept_risky_margin
        and raw_support_ratio >= 0.999
    )
    crops_or_consensus = (n_crops >= accept_min_crops) or strong_multi_variant or confident_single

    g = {
        "final_score": _mk(final_score >= accept_final_score, round(final_score, 4), accept_final_score),
        "crops_or_consensus": _mk(
            crops_or_consensus, n_crops, accept_min_crops,
            extra=f"reads={n_reads}, strong_multi={strong_multi_variant}, confident_single={confident_single}"),
        "structurally_valid": _mk(bool(best["fully"]), round(best["compliance"], 3), 1.0),
        "risky_margin": _mk(min_risky_margin >= accept_risky_margin,
                            round(min_risky_margin, 3), accept_risky_margin),
        "variable_margin": _mk(min_variable_margin >= accept_variable_margin,
                                round(min_variable_margin, 3), accept_variable_margin),
        "serial_margin": _mk(min_serial_margin >= accept_serial_margin,
                              round(min_serial_margin, 3), accept_serial_margin),
        "exact_conflict": _mk(not exact.get("conflict", False),
                              exact.get("competing_candidates", []), "no trusted full-VIN conflict"),
        "raw_support_ratio": _mk(raw_support_ratio >= accept_raw_support_ratio,
                                 round(raw_support_ratio, 3), accept_raw_support_ratio),
        "known_model": _mk((model_final is not None) or (not require_known_model),
                           model_final or "None", "QY|BL7M"),
        "pos5": _mk(pos5_ok, pos5_audit.get("chosen"), "hard-gate", extra=";".join(pos5_reasons)),
    }

    failed = {k for k, v in g.items() if not v["passed"]}
    exact_direct_rescued = False
    if failed and failed <= {"pos5", "risky_margin", "variable_margin"}:
        chosen5 = decisions[POS5].chosen_char
        direct_allowed_competitors = [
            ch for ch in votes[POS5]
            if ch != chosen5 and vin_rules.is_allowed(model_final, POS5, ch)
        ] if model_final else []
        exact_direct_rescued = (
            bool(best["fully"])
            and bool(pos5_audit.get("directly_seen"))
            and not direct_allowed_competitors
            and not exact.get("conflict", False)
            and exact.get("exact_mean_conf", 0.0) >= exact_rescue_min_conf
            and (exact.get("exact_groups", 0) >= 2
                 or len(exact.get("trusted_bases", [])) >= 2)
        )
        if exact_direct_rescued:
            reason = (f"direct exact consensus: groups={exact.get('exact_groups', 0)}, "
                      f"trusted={exact.get('trusted_bases', [])}, "
                      f"mean_conf={exact.get('exact_mean_conf', 0.0):.3f}")
            pos5_audit["hard_gate_passed"] = bool(pos5_audit.get("passed"))
            pos5_audit["passed"] = True
            pos5_audit["rescued"] = True
            pos5_audit["rescue_reason"] = reason
            for key in failed:
                g[key]["passed"] = True
                g[key]["extra"] = reason

    failed = {k for k, v in g.items() if not v["passed"]}
    pos5_rescued = False
    if pos5_structure_rescue_enabled and failed and failed <= {
            "pos5", "risky_margin", "variable_margin"}:
        raw5 = str(pos5_audit.get("raw_direct") or "")
        chosen5 = str(pos5_audit.get("chosen") or "")
        allowed5 = chosen5 in vin_rules.allowed_at(model_final, POS5) if model_final else False
        rescue_raws = set(str(pos5_structure_rescue_raw_chars or ""))
        # Rescue faqat TASVIR DALILI bor exact VIN uchun. Kamida ikki mustaqil
        # crop yoki raw+CLAHE oilalari kelishuvi kerak; strict full-VIN competitor
        # bo'lsa rescue qat'iyan o'chadi.
        exact_evidence_ok = (
            exact.get("exact_mean_conf", 0.0) >= exact_rescue_min_conf
            and (exact.get("exact_groups", 0) >= 2
                 or len(exact.get("trusted_bases", [])) >= 2)
            and not exact.get("conflict", False)
        )
        pos5_rescued = (
            "pos5" in failed
            and bool(best["fully"])
            and model_final is not None
            and bool(pos5_audit.get("structure_corrected"))
            and allowed5
            and raw5 in rescue_raws
            and final_score >= pos5_structure_rescue_min_score
            and raw_support_ratio >= pos5_structure_rescue_min_raw_support
            and exact_evidence_ok
        )
        if pos5_rescued:
            reason = (f"rescued raw_pos5='{raw5}' -> '{chosen5}', "
                      f"score={final_score:.3f}, raw_support={raw_support_ratio:.3f}")
            pos5_audit["hard_gate_passed"] = bool(pos5_audit.get("passed"))
            pos5_audit["passed"] = True
            pos5_audit["rescued"] = True
            pos5_audit["rescue_reason"] = reason
            g["pos5"]["passed"] = True
            g["pos5"]["extra"] = reason
            if "risky_margin" in failed:
                g["risky_margin"]["passed"] = True
                g["risky_margin"]["extra"] = reason
            if "variable_margin" in failed:
                g["variable_margin"]["passed"] = True
                g["variable_margin"]["extra"] = reason

    accepted = all(v["passed"] for v in g.values())
    reasons = [f"{k}: {v.get('extra') or v['value']}" for k, v in g.items() if not v["passed"]]

    return FusionResult(
        status=("ACCEPT" if accepted else "OCR_AMBIGUOUS"),
        validated_vin=validated, model=model_final, final_score=float(final_score),
        compliance=float(best["compliance"]), fully_compliant=bool(best["fully"]),
        raw_support_ratio=float(raw_support_ratio), n_crops=n_crops, n_reads=n_reads,
        decisions=decisions, pos5_audit=pos5_audit,
        gate={**g, "evidence": exact}, reasons=reasons)
