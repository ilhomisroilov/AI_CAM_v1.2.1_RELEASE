"""
tests/test_retention.py
============================================================
P2 — Retention yo'q (audit finding: dataset/collected_raw 458MB/215 fayl,
data/crops 8MB/110 fayl, hech qachon tozalanmaydi).

BARCHA testlar FAQAT tmp_path ICHIDAGI soxta rootlarda ishlaydi — HAQIQIY
data/crops yoki dataset/collected_raw ga HECH QACHON tegilmaydi.
"""
from __future__ import annotations

import os
import time

from backend.server import _dir_retention_scan, apply_retention


def _touch(path, age_seconds: float, size_bytes: int = 100):
    path.write_bytes(b"x" * size_bytes)
    old = time.time() - age_seconds
    os.utime(path, (old, old))


def test_dry_run_deletes_nothing(tmp_path):
    root = tmp_path / "crops"
    root.mkdir()
    old_file = root / "old.jpg"
    _touch(old_file, age_seconds=40 * 86400)   # 40 kun eski

    report = apply_retention(root, max_age_days=30, max_total_mb=1024,
                             active_grace_minutes=0, dry_run=True)

    assert report["dry_run"] is True
    assert str(old_file.resolve()) in report["candidates"]
    assert report["deleted"] == []
    assert old_file.exists()   # HECH NARSA o'chirilmagan


def test_real_retention_deletes_only_candidates_within_root(tmp_path):
    root = tmp_path / "crops"
    root.mkdir()
    old_file = root / "old.jpg"
    fresh_file = root / "fresh.jpg"
    _touch(old_file, age_seconds=40 * 86400)
    _touch(fresh_file, age_seconds=1 * 86400)

    outside_file = tmp_path / "outside.jpg"   # root TASHQARISIDA
    _touch(outside_file, age_seconds=100 * 86400)

    report = apply_retention(root, max_age_days=30, max_total_mb=1024,
                             active_grace_minutes=0, dry_run=False)

    assert not old_file.exists()          # eski fayl o'chirildi
    assert fresh_file.exists()            # yangi fayl saqlanib qoldi
    assert outside_file.exists()          # root tashqarisidagi faylga TEGILMADI
    assert str(old_file.resolve()) in report["deleted"]


def test_active_grace_period_protects_recent_files(tmp_path):
    root = tmp_path / "crops"
    root.mkdir()
    just_written = root / "just_now.jpg"
    _touch(just_written, age_seconds=5)   # 5 soniya oldin yozilgan

    report = apply_retention(root, max_age_days=0, max_total_mb=0,
                             active_grace_minutes=15, dry_run=False)

    assert just_written.exists()   # grace davrida — TEGILMAYDI
    assert str(just_written.resolve()) not in report["deleted"]


def test_max_total_size_triggers_cleanup_oldest_first(tmp_path):
    root = tmp_path / "crops"
    root.mkdir()
    f1 = root / "a.jpg"; _touch(f1, age_seconds=300, size_bytes=1024 * 1024)
    f2 = root / "b.jpg"; _touch(f2, age_seconds=200, size_bytes=1024 * 1024)
    f3 = root / "c.jpg"; _touch(f3, age_seconds=100, size_bytes=1024 * 1024)

    # jami 3MB, limit 1MB -> eng eskilari (a, b) nomzod bo'lishi kerak
    report = _dir_retention_scan(root, max_age_days=0, max_total_mb=1,
                                 active_grace_minutes=0)
    assert str(f1.resolve()) in report["candidates"]
    assert str(f2.resolve()) in report["candidates"]
    assert str(f3.resolve()) in report["kept"]


def test_apply_retention_never_touches_files_outside_root_even_with_crafted_candidate(tmp_path, monkeypatch):
    """
    Himoya qatlami: apply_retention() o'zining ICHKI scan natijasidan tashqari
    hech narsaga ishonmaydi — _dir_retention_scan natijasini "candidates" ga
    tashqi (root dan tashqari) yo'l bilan qalbakilashtirsak ham, o'chirish
    bosqichi rp.relative_to(root) tekshiruvi bilan bunday fayllarni RAD ETADI.
    """
    import backend.server as server_mod

    root = tmp_path / "crops"
    root.mkdir()
    outside = tmp_path / "not_in_root.txt"
    outside.write_text("keep me")

    real_scan = server_mod._dir_retention_scan

    def _crafted_scan(*args, **kwargs):
        report = real_scan(*args, **kwargs)
        report["candidates"] = [str(outside)]   # xavfsizlik tekshiruvini sinash uchun tashqi yo'l
        return report

    monkeypatch.setattr(server_mod, "_dir_retention_scan", _crafted_scan)
    apply_retention(root, max_age_days=0, max_total_mb=0, active_grace_minutes=0, dry_run=False)
    assert outside.exists()   # root tashqarisidagi faylga TEGILMADI
