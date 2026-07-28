"""v1.2.1 stabilization — observation run-metadata + morning preflight helpers."""
from __future__ import annotations

import json


def test_run_metadata_captures_build_identity():
    from tools.observe_production import run_metadata
    md = run_metadata()
    assert md["version"] == "1.2.1"
    assert set(md["model_hashes"]) >= {"yolo", "engraved_onnx", "paddle_det", "paddle_rec", "paddle_cls"}
    # config hash present (models may be absent in a bare checkout, but this one has them)
    assert md["config_hash"]


def test_metadata_embedded_in_reports(tmp_path):
    from tools.observe_production import collect_metrics, generate_reports, run_metadata
    m = collect_metrics(tmp_path / "audit", tmp_path / "db.sqlite", tmp_path / "coll")
    out = tmp_path / "reports"
    generate_reports(m, out, completed=False, duration_hours=24.0, elapsed_hours=0.0,
                     metadata=run_metadata())
    j = json.loads((out / "PRODUCTION_24H_METRICS.json").read_text(encoding="utf-8"))
    assert "run_metadata" in j and j["run_metadata"]["version"] == "1.2.1"
    audit = (out / "PRODUCTION_24H_AUDIT.md").read_text(encoding="utf-8")
    assert "Build (frozen for the window)" in audit
    assert "NOT COMPLETED" in audit


def test_preflight_check_aggregation_and_tcp():
    from tools import morning_preflight as mp
    c = mp.Check()
    c.add("ok", True)
    c.add("soft-fail", False, hard=False)   # not a blocker
    c.add("hard-fail", False, hard=True)    # blocker
    blockers = c.blockers()
    assert [b[0] for b in blockers] == ["hard-fail"]
    # a definitely-closed port is unreachable
    assert mp._tcp("127.0.0.1", 59993, timeout=0.3) is False


def test_preflight_version_check_expects_1_2_1():
    from tools import morning_preflight as mp
    assert mp.EXPECTED_VERSION == "1.2.1"
