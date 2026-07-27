"""
============================================================
vin_postprocess.py  —  VIN-aware OCR post-processing (candidate ranking)
============================================================
Maqsad: OCR natijasini O'ZGARTIRMASDAN, eng EHTIMOLLI VIN nomzodini
tanlash. Tizim belgini O'YLAB TOPMAYDI / MAJBURAN ALMASHTIRMAYDI — u faqat
OCR KO'RGAN belgilar (va ularning vizual chalkashliklari) orasidan VIN
strukturasiga ko'ra SARALAYDI.

Kafolat ("never invent"):
  Har pozitsiya uchun nomzodlar = { OCR belgisi } ∪ { OCR belgisining
  ma'lum chalkashlik variantlari }. Kutilgan strukturaviy belgi FAQAT shu
  to'plamda bo'lsa tanlanishi mumkin. Aks holda OCR belgisi saqlanadi
  (jazo bilan belgilanadi). Xom OCR natijasi HAR DOIM saqlanadi.

Ball:  char_score = w_visual*visual + (struct_bonus | -struct_penalty)
       FinalScore = 0.5*mean(chosen_visual) + 0.5*compliance_fraction
Global ketma-ketlik bali = pozitsiyalar bo'yicha additiv (strukturaviy
qoidalar pozitsiyaga bog'liq -> per-position argmax = global optimum).

MUHIM (Section 6): bu modul BITTA o'qish uchun ranker. YAKUNIY qabul qarori
position-level consensus (vin_fusion.py) + global accept gate orqali beriladi.
`fully_compliant` FAQAT "strukturaviy jihatdan yaroqli" degani — o'zi qabul
qarori EMAS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import vin_rules

# --- OCR vizual chalkashliklar (ikki tomonlama) ---
# P14 FIX: etched/dot-peen metall VIN uchun keng tarqalgan misread juftlari
# kengaytirildi (Z↔2, D↔0, 5↔6, 2↔7, 8↔0, B↔3, 7↔1...). "Never invent" siyosati
# saqlanadi: strukturaviy belgi FAQAT OCR belgisining chalkashlik to'plamida
# bo'lsa tanlanadi. (I/O/Q VIN da yo'q — lekin O/0, I/1 OCR darajasida bo'ladi.)
CONFUSIONS: Dict[str, Tuple[str, ...]] = {
    "0": ("O", "D", "Q", "8"),
    "1": ("I", "T", "7", "L", "J"),     # pos11 J ko'pincha 1 deb o'qiladi
    "2": ("Z", "7"),
    "3": ("8", "B"),
    "4": ("A",),
    "5": ("S", "6"),
    "6": ("G", "5", "8"),
    "7": ("1", "2", "T"),
    "8": ("B", "0", "3", "6"),
    "9": ("0",),
    "A": ("4",),
    "B": ("8", "3", "R"),
    "D": ("0", "O"),
    "E": ("F",),
    "F": ("E", "P"),
    "G": ("6", "C"),
    "I": ("1", "J"),
    "L": ("1", "J"),
    "O": ("0", "D", "Q"),
    "Q": ("0", "O"),
    "R": ("B",),
    "S": ("5",),
    "T": ("1", "7"),
    "Z": ("2",),
}


@dataclass
class PositionDecision:
    pos: int                 # 1-asosli pozitsiya
    raw_char: str
    chosen_char: str
    visual: float
    allowed: bool            # tanlangan belgi strukturaga mosmi
    changed: bool            # OCR belgisidan farq qiladimi (re-rank/enforce natijasi)
    # --- Audit (Section 6): consensus qo'llab-quvvatlashi ---
    support_count: int = 0           # tanlangan belgini TO'G'RIDAN-TO'G'RI ko'rgan OCR o'qishlari soni
    margin: float = 0.0              # top va 2-belgi orasidagi ishonch farqi (0..1)
    source_variants: List[str] = field(default_factory=list)  # qaysi variantlar shu belgini bergani
    structure_corrected: bool = False  # belgi FAQAT struktura tufayli tanlandimi (xom OCR ko'rmagan)


@dataclass
class VinResult:
    raw_vin: str             # xom OCR (audit uchun saqlanadi)
    validated_vin: str       # strukturaga ko'ra saralangan natija
    model: Optional[str]
    final_score: float       # 0..1
    compliance: float        # qoidaga mos pozitsiyalar ulushi 0..1
    fully_compliant: bool
    decisions: List[PositionDecision] = field(default_factory=list)
    note: str = ""


def _candidates(ch: str, visual: float, confusion_prior: float) -> Dict[str, float]:
    """OCR belgisi + uning chalkashlik variantlari (past vizual ball bilan)."""
    cands: Dict[str, float] = {ch: visual}
    for alt in CONFUSIONS.get(ch, ()):  # faqat ma'lum chalkashliklar
        v = visual * confusion_prior
        if v > cands.get(alt, -1.0):
            cands[alt] = v
    return cands


def _is_variable_position(model: str, pos0: int) -> bool:
    """
    O'ZGARUVCHAN pozitsiya = ruxsat etilgan to'plamда >1 belgi (mas. pos5={A,B,C,D,G,H,J},
    pos9={A,E}, pos10={T,V,W}) YOKI raqamli pozitsiya (12-17, 10 xil raqam). Bunda
    struktura O'ZI belgini tanlab qo'ymasligi kerak — rasm dalili hal qiladi.
    """
    try:
        return len(vin_rules.allowed_at(model, pos0)) > 1
    except Exception:
        return False


def postprocess(
    raw_vin: str,
    model: Optional[str],
    char_confidences: Optional[List[float]] = None,
    overall_conf: float = 0.0,
    *,
    confusion_prior: float = 0.6,
    w_visual: float = 1.0,
    struct_bonus: float = 0.5,
    struct_penalty: float = 0.5,
    struct_bonus_variable: Optional[float] = None,
    enforce_fixed_positions: bool = True,
) -> VinResult:
    """
    Xom OCR natijasini model qoidalariga ko'ra saralaydi (bitta o'qish uchun ranker).

    MUHIM (Section 6): `fully_compliant` FAQAT "strukturaviy jihatdan yaroqli"
    degani — u YAKUNIY qabul qarori EMAS. Qabul qarori position-level consensus
    (vin_fusion.py) va global accept gate orqali beriladi.

    struct_bonus_variable: o'zgaruvchan pozitsiyalar (ayniqsa pos5) uchun KAMAYTIRILGAN
      struktura bonusi. None -> struct_bonus ishlatiladi (orqaga moslik). Bu berilganда
      pos5 kabi pozitsiyada struktura belgini majburan tanlab qo'ymaydi.

    char_confidences: agar mavjud bo'lsa, har belgi uchun vizual ishonch
      (PaddleOCR yuqori-darajali API buni bermaydi -> overall_conf ishlatiladi).
    """
    bonus_var = struct_bonus if struct_bonus_variable is None else float(struct_bonus_variable)
    raw = "".join(c for c in raw_vin.upper() if c.isalnum())

    # Model qo'llab-quvvatlanmasa yoki uzunlik 17 emas -> validatsiya qilmaymiz
    if not vin_rules.is_supported_model(model) or len(raw) != vin_rules.VIN_LENGTH:
        return VinResult(
            raw_vin=raw, validated_vin=raw, model=model,
            final_score=float(overall_conf), compliance=0.0, fully_compliant=False,
            note=("noma'lum model" if not vin_rules.is_supported_model(model)
                  else f"uzunlik {len(raw)} != 17 — validatsiya o'tkazilmadi"),
        )

    confs = char_confidences if (char_confidences and len(char_confidences) == len(raw)) \
        else [overall_conf] * len(raw)

    chosen: List[str] = []
    chosen_visual: List[float] = []
    decisions: List[PositionDecision] = []

    for i, ch in enumerate(raw):
        cands = _candidates(ch, float(confs[i]), confusion_prior)
        # O'zgaruvchan pozitsiyada struktura bonusini kamaytiramiz (pos5 himoyasi).
        pos_bonus = bonus_var if _is_variable_position(model, i) else struct_bonus
        best_char, best_score, best_vis = ch, -1e9, float(confs[i])
        for cand, vis in cands.items():
            struct = pos_bonus if vin_rules.is_allowed(model, i, cand) else -struct_penalty
            score = w_visual * vis + struct
            if score > best_score:
                best_char, best_score, best_vis = cand, score, vis
        # Struktura-tuzatish: belgi xom OCR belgisidan FARQ qildi va bu farq
        # strukturaga moslashtirish uchun (xom belgi ruxsat etilmagan, yangisi ruxsat).
        structure_only = (best_char != ch
                          and vin_rules.is_allowed(model, i, best_char)
                          and not vin_rules.is_allowed(model, i, ch))
        # STANDART: model-mustaqil DOIMIY pozitsiyalar (1=N,2=S,3=T,7=1,11=J) —
        # OCR boshqa belgi o'qisa ham majburan standart belgi qo'yiladi (changed=true).
        if enforce_fixed_positions and i in vin_rules.CONSTANT_POSITIONS:
            forced = vin_rules.CONSTANT_POSITIONS[i]
            structure_only = (forced != ch)
            best_char = forced
        chosen.append(best_char)
        chosen_visual.append(best_vis)
        decisions.append(PositionDecision(
            pos=i + 1, raw_char=ch, chosen_char=best_char, visual=best_vis,
            allowed=vin_rules.is_allowed(model, i, best_char),
            changed=(best_char != ch),
            structure_corrected=bool(structure_only),
        ))

    validated = "".join(chosen)
    fully, _flags = vin_rules.validate(model, validated)
    compliance = vin_rules.compliance_fraction(model, validated)
    mean_vis = sum(chosen_visual) / len(chosen_visual) if chosen_visual else 0.0
    final = 0.5 * mean_vis + 0.5 * compliance

    return VinResult(
        raw_vin=raw, validated_vin=validated, model=model,
        final_score=float(final), compliance=float(compliance), fully_compliant=fully,
        decisions=decisions,
    )
