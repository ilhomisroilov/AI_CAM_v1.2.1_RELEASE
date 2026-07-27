"""
============================================================
test_ocr_engine_config.py — v1.2.1 engine config, migration, health
============================================================
Covers the v1.2.1 acceptance items:
  12. Engine switching does not regress queued-trigger handover (router build is
      independent of pipeline session/queue state).
  16. Config migration from the current version works (legacy config -> safe defaults).
  17. Legacy / invalid configuration fails CLEARLY (validate_config reports it) but
      never crashes startup.
  18. /health reports engine / model / version / readiness.
"""
from __future__ import annotations

import dataclasses

import backend.config as config_mod
from backend.ai import ocr_contract as C
from backend.ai.engines import (
    EngravedSequenceRecognizer,
    PaddleOCRLegacyAdapter,
    build_engine,
    build_router_from_config,
    engines_health_report,
)


# ---------------------------------------------------------------------------
# 16. Config migration / backward compatibility
# ---------------------------------------------------------------------------
def test_default_config_is_legacy_paddle_only():
    """Absent/empty ocr_engines -> legacy PaddleOCR primary, no shadow/fallback."""
    cfg = config_mod.OCR_ENGINES
    assert cfg.primary_engine == "paddleocr_legacy"
    assert str(cfg.shadow_engine).strip() in ("", "none", "null", "disabled")
    assert str(cfg.fallback_engine).strip() in ("", "none", "null", "disabled")
    assert cfg.charset_profile == "alphanumeric_36"


def test_legacy_config_without_ocr_engines_section_uses_defaults():
    """A settings dict from an OLDER version (no ocr_engines key) must not crash and
    must leave the defaults intact — this is the migration path."""
    legacy_cfg = config_mod.OCREnginesConfig()   # fresh defaults, as if unspecified
    router = build_router_from_config(legacy_cfg)
    assert isinstance(router.primary, PaddleOCRLegacyAdapter)
    assert router.shadow is None
    assert router.fallback is None


def test_build_router_default_is_legacy_only():
    router = build_router_from_config()
    assert isinstance(router.primary, PaddleOCRLegacyAdapter)
    assert router.shadow is None and router.fallback is None


def test_enabling_engraved_shadow_builds_disabled_placeholder():
    cfg = dataclasses.replace(config_mod.OCREnginesConfig(),
                              shadow_engine="engraved_sequence")
    router = build_router_from_config(cfg)
    assert isinstance(router.primary, PaddleOCRLegacyAdapter)
    assert isinstance(router.shadow, EngravedSequenceRecognizer)
    # even enabled as shadow, it is not configured (no model artifact)
    assert router.shadow.health().status == "not_configured"


def test_build_engine_disabled_tokens_return_none():
    cfg = config_mod.OCREnginesConfig()
    for token in ("", "none", "null", "disabled", "NONE"):
        assert build_engine(token, cfg) is None


def test_build_engine_unknown_id_raises():
    cfg = config_mod.OCREnginesConfig()
    try:
        build_engine("totally_unknown_engine", cfg)
        assert False, "expected ValueError for unknown engine id"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# 17. Invalid config fails clearly (does not crash)
# ---------------------------------------------------------------------------
def test_invalid_primary_engine_is_reported_not_crashing(monkeypatch):
    monkeypatch.setattr(config_mod.OCR_ENGINES, "primary_engine", "bogus_engine")
    issues = config_mod.validate_config()
    assert any("ocr_engines.primary_engine" in i for i in issues)


def test_invalid_charset_profile_is_reported(monkeypatch):
    monkeypatch.setattr(config_mod.OCR_ENGINES, "charset_profile", "utf8_lol")
    issues = config_mod.validate_config()
    assert any("ocr_engines.charset_profile" in i for i in issues)


def test_invalid_shadow_engine_is_reported(monkeypatch):
    monkeypatch.setattr(config_mod.OCR_ENGINES, "shadow_engine", "nope")
    issues = config_mod.validate_config()
    assert any("ocr_engines.shadow_engine" in i for i in issues)


def test_empty_shadow_engine_is_valid(monkeypatch):
    monkeypatch.setattr(config_mod.OCR_ENGINES, "shadow_engine", "")
    issues = config_mod.validate_config()
    assert not any("ocr_engines.shadow_engine" in i for i in issues)


# ---------------------------------------------------------------------------
# 12. Router build does not touch pipeline session/queue state
# ---------------------------------------------------------------------------
def test_router_build_is_independent_of_pipeline_state():
    """Switching engine config builds a router without importing/altering the
    pipeline — so it cannot regress queued-trigger handover."""
    import ast
    import backend.ai.engines.factory as fac
    with open(fac.__file__, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("pipeline" in m for m in imported)
    # building routers repeatedly is side-effect free
    r1 = build_router_from_config()
    r2 = build_router_from_config()
    assert r1 is not r2
    assert isinstance(r1.primary, PaddleOCRLegacyAdapter)


# ---------------------------------------------------------------------------
# 18. Engines health report
# ---------------------------------------------------------------------------
def test_engines_health_report_reports_engine_model_version_readiness():
    report = engines_health_report()
    assert isinstance(report, list) and report
    primary = next(e for e in report if e["mode"] == "primary")
    assert primary["engine_id"] == "paddleocr_legacy"
    assert "engine_version" in primary
    assert "engine_model_id" in primary
    assert "charset_id" in primary
    assert "readiness" in primary


def test_engines_health_report_includes_shadow_when_configured():
    cfg = dataclasses.replace(config_mod.OCREnginesConfig(),
                              shadow_engine="engraved_sequence")
    report = engines_health_report(cfg)
    shadow = next(e for e in report if e["mode"] == "shadow")
    assert shadow["engine_id"] == "engraved_sequence"
    assert shadow["readiness"] == "not_configured"


def test_health_endpoint_exposes_ocr_engines(client, monkeypatch):
    """18. /health carries components.ocr.engines with engine/version/readiness."""
    monkeypatch.setattr(config_mod.AUTH, "enabled", False)
    r = client.get("/health")
    assert r.status_code == 200
    engines = r.json()["components"]["ocr"].get("engines")
    assert isinstance(engines, list) and engines
    primary = next(e for e in engines if e.get("mode") == "primary")
    assert primary["engine_id"] == "paddleocr_legacy"
    assert "engine_version" in primary
    assert "readiness" in primary
