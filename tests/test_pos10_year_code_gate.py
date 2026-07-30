"""
pos10 (yil kodi) qattiq gate — hotfix RC1 regressiya testlari.

Asosiy himoya: STRUKTURA yil kodini O'YLAB TOPMASLIGI kerak.

Sintetik regressiya ssenariysida pos10 da xom OCR chalkash belgini ko'p marta,
valid yil kodini esa boshqa o'qishda ko'radi. Final nomzodning yil kodi bevosita
kuzatilmagan bo'lsa, fusion uni qabul qilmasligi kerak.

Bu testlar shu mexanizmni bevosita, takrorlanadigan shaklda qulflaydi.
"""
from __future__ import annotations

import pytest

from backend.ai.vin_fusion import fuse, VariantRead, POS10

# Entirely synthetic test value; not copied from production evidence.
SYNTHETIC_SESSION_ID = "synthetic-pos10-direct-evidence-test"
SYNTHETIC_PREFIX = "NSTFH814E"
SYNTHETIC_YEAR_CODE = "V"
SYNTHETIC_SUFFIX = "J"
SYNTHETIC_SERIAL = "900001"


def _synthetic_vin(year_code: str = SYNTHETIC_YEAR_CODE) -> str:
    return (
        SYNTHETIC_PREFIX
        + year_code
        + SYNTHETIC_SUFFIX
        + SYNTHETIC_SERIAL
    )


# QY: pos10 ∈ {T,V,W}. `U` ruxsat etilmagan.
QY_BASE = _synthetic_vin()
GATE_OFF = dict(pos10_require_direct=False, pos10_min_direct_share=0.0,
                pos10_min_crops=0, pos10_min_variants=0)


def _mk(text: str, n: int, conf: float = 0.95, group: int = 0,
        variant: str = "raw_resized", start: int = 0):
    """n ta bir xil o'qish (turli variant nomlari bilan)."""
    out = []
    for i in range(n):
        out.append(VariantRead(
            crop_index=group, variant_name=f"{variant}#{start + i}",
            ocr_conf=conf, raw_text=text, normalized_raw=text,
            evidence_group=group))
    return out


def _sub(text: str, pos: int, ch: str) -> str:
    return text[:pos] + ch + text[pos + 1:]


def _reads_with_pos10(raw_char: str, *, n: int, groups: int = 2, conf: float = 0.95):
    """pos10 da `raw_char` o'qilgan, qolgan barcha pozitsiyalar toza va bir xil."""
    text = _sub(QY_BASE, POS10, raw_char)
    reads = []
    per = max(1, n // max(1, groups))
    for g in range(groups):
        variant = "raw_resized" if g % 2 == 0 else "clahe_unsharp"
        reads.extend(_mk(text, per, conf=conf, group=g, variant=variant, start=g * 10))
    return reads


class TestStructureMustNotInventYearCode:
    """F-1: eng muhim himoya."""

    def test_baseline_invents_T_from_raw_1(self):
        """Gate O'CHIRILGANDA eski xatti-harakat qayta ishlab chiqariladi:
        xom `1` o'qilgan, `T` tanlangan (ya'ni o'ylab topilgan)."""
        reads = _reads_with_pos10("1", n=8, groups=2)
        fr = fuse(reads, **GATE_OFF)
        assert fr.pos10_audit["chosen"] == "T", fr.pos10_audit
        assert fr.pos10_audit["directly_seen"] is False
        # Eski mantiqda bu ACCEPT bo'lib yakuniy VIN ga tushardi.
        assert fr.status == "ACCEPT", (
            "baseline bu holatda ACCEPT bo'lishi kerak — aks holda test "
            f"F-1 ni namoyish qilmaydi: {fr.reasons}")
        assert fr.validated_vin[POS10] == "T"

    def test_rc1_blocks_invented_T(self):
        """RC1: aynan shu kirish AMBIGUOUS bo'ladi va sabab aniq ko'rsatiladi."""
        reads = _reads_with_pos10("1", n=8, groups=2)
        fr = fuse(reads)
        assert fr.status != "ACCEPT", "o'ylab topilgan T QABUL QILINDI — SOXTA QABUL!"
        assert fr.gate["pos10"]["passed"] is False
        assert "struktura-ixtiro" in fr.gate["pos10"]["extra"]
        assert fr.pos10_audit["directly_seen"] is False

    def test_rc1_blocks_invented_T_even_with_many_variants(self):
        """Ko'p variant/crop ham o'ylab topilgan belgini QUTQARMAYDI
        (`strong_multi_variant` yo'li pos10 ni ko'tarib ketmasligi kerak)."""
        reads = _reads_with_pos10("1", n=18, groups=3, conf=0.99)
        fr = fuse(reads)
        assert fr.status != "ACCEPT", (
            "ko'p variant o'ylab topilgan T ni qabul qildi — rescue teshigi bor!")
        assert fr.gate["pos10"]["passed"] is False

    def test_rescue_paths_cannot_override_pos10(self):
        """pos10 yiqilganda rescue to'plamlari ishga tushmasligi kerak."""
        reads = _reads_with_pos10("1", n=12, groups=2, conf=0.99)
        fr = fuse(reads)
        assert fr.gate["pos10"]["passed"] is False
        # rescue faqat {pos5, risky_margin, variable_margin} uchun; pos10 unda yo'q.
        assert fr.status != "ACCEPT"


class TestDirectlySeenYearCodeStillAccepted:
    """Gate ATAYLAB tor: to'g'ridan-to'g'ri ko'rilgan yil kodini buzmaydi."""

    @pytest.mark.parametrize("ch", ["V", "T"])
    def test_direct_year_code_accepted(self, ch):
        reads = _reads_with_pos10(ch, n=8, groups=2)
        fr = fuse(reads)
        assert fr.pos10_audit["directly_seen"] is True
        assert fr.gate["pos10"]["passed"] is True, fr.gate["pos10"]
        assert fr.status == "ACCEPT", fr.reasons
        assert fr.validated_vin[POS10] == ch

    def test_disallowed_U_does_not_count_as_contrary_for_QY(self):
        """QY da `U` struktura bo'yicha mumkin emas va engraved shriftda `V` ga
        vizual jihatdan teng — shuning uchun to'g'ridan-to'g'ri ko'rilgan `V` ni
        BLOKLAMAYDI (ataylab qabul qilingan qaror, F-2 tahlili)."""
        reads = _reads_with_pos10("V", n=4, groups=2)
        reads += _reads_with_pos10("U", n=6, groups=2)
        fr = fuse(reads)
        assert fr.pos10_audit["chosen"] == "V"
        assert fr.pos10_audit["directly_seen"] is True
        assert fr.pos10_audit["allowed_contrary"] == []
        assert fr.gate["pos10"]["passed"] is True


class TestVerifierIsVetoOnly:
    """Geometrik verifier kalibrlanmagan (WORKER_2 B-1) — u FAQAT veto qiladi."""

    def test_verifier_cannot_confirm_unseen_char(self):
        """Verifier BAR_TOP desa ham, ko'rilmagan `T` QABUL QILINMAYDI."""
        reads = _reads_with_pos10("1", n=8, groups=2)
        fr = fuse(reads, pos10_verifier_verdict="BAR_TOP")
        assert fr.status != "ACCEPT", (
            "verifier ko'rilmagan belgini tasdiqlab qo'ydi — bu taqiqlangan!")

    def test_verifier_veto_blocks_contradicted_T(self):
        """To'g'ridan-to'g'ri ko'rilgan `T` ni verifier shakli DOUBLE_VERTEX
        deb ziddiyat bildirsa — AMBIGUOUS."""
        reads = _reads_with_pos10("T", n=8, groups=2)
        ok = fuse(reads, pos10_verifier_verdict="BAR_TOP")
        assert ok.gate["pos10"]["passed"] is True
        vetoed = fuse(reads, pos10_verifier_verdict="DOUBLE_VERTEX")
        assert vetoed.gate["pos10"]["passed"] is False
        assert "veto" in vetoed.gate["pos10"]["extra"]

    def test_verifier_absent_is_fail_open(self):
        """Verifier ishlamasa (NOT_RUN) bugungi xatti-harakat saqlanadi."""
        reads = _reads_with_pos10("V", n=8, groups=2)
        fr = fuse(reads)
        assert fr.pos10_audit["verifier_verdict"] == "NOT_RUN"
        assert fr.status == "ACCEPT"


class TestNoReadUnchanged:
    def test_empty_reads_still_no_read(self):
        fr = fuse([])
        assert fr.status != "ACCEPT"
