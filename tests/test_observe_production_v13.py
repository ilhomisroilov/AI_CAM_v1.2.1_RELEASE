"""v1.3.0 item 12 — 24h observation aggregation + report generation."""
from __future__ import annotations

import csv
import json
import sqlite3

from tools.observe_production import collect_metrics, generate_reports


def _write_plc_csv(audit_dir):
    p = audit_dir / "plc_edges_20260728.csv"
    fields = ["edge_type", "decision", "time_since_prev_rising_ms", "pulse_width_ms"]
    rows = [
        {"edge_type": "rising", "decision": "ACCEPTED", "time_since_prev_rising_ms": "", "pulse_width_ms": ""},
        {"edge_type": "falling", "decision": "FALLING", "time_since_prev_rising_ms": "", "pulse_width_ms": "3000"},
        {"edge_type": "rising", "decision": "SUSPICIOUS_EARLY_TRIGGER", "time_since_prev_rising_ms": "20000", "pulse_width_ms": ""},
        {"edge_type": "rising", "decision": "ACCEPTED", "time_since_prev_rising_ms": "95000", "pulse_width_ms": ""},
        {"edge_type": "falling", "decision": "INVALID_PLC_PULSE", "time_since_prev_rising_ms": "", "pulse_width_ms": "800"},
    ]
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)


def _write_suppressed(audit_dir):
    p = audit_dir / "suppressed_triggers_20260728.jsonl"
    with p.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"decision": "DUPLICATE_TRIGGER_IGNORED"}) + "\n")
        f.write(json.dumps({"decision": "SUSPICIOUS_EARLY_TRIGGER"}) + "\n")


def _write_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE vin_records (id INTEGER PRIMARY KEY, detected_vin TEXT, status TEXT, image_path TEXT)")
    conn.executemany("INSERT INTO vin_records (detected_vin, status, image_path) VALUES (?,?,?)", [
        ("NSTFA814ATJ123456", "SUCCESS", "crops/a.jpg"),
        ("NO_READ", "NO_READ", "crops/novin.jpg"),      # evidence present
        ("NO_READ", "TIMEOUT", ""),                       # evidence MISSING
        ("NSTFA814ATJ123456", "SUCCESS", "crops/b.jpg"),  # duplicate VIN
    ])
    conn.execute("CREATE TABLE ocr_engine_results (session_id TEXT, engine_name TEXT, gated_text TEXT, latency_ms REAL)")
    conn.executemany("INSERT INTO ocr_engine_results VALUES (?,?,?,?)", [
        ("S1", "ENGRAVED_V121", "NSTU", 12.0),
        ("S1", "PADDLE_RAW", "NSTV", 30.0),   # U vs V conflict at pos 3
        ("S1", "PADDLE_ENHANCED", "NSTV", 28.0),
    ])
    conn.commit(); conn.close()


def test_collect_and_report(tmp_path):
    audit = tmp_path / "audit"; audit.mkdir()
    _write_plc_csv(audit); _write_suppressed(audit)
    db = tmp_path / "ai_cam.db"; _write_db(db)
    coll = tmp_path / "collection"  # absent -> zeros
    out = tmp_path / "reports"

    m = collect_metrics(audit, db, coll)
    assert m["plc"]["total_rising_edges"] == 3
    assert m["plc"]["accepted_rising_edges"] == 2
    assert m["plc"]["rejected_early_edges"] == 1
    assert m["plc"]["invalid_pulse_widths"] == 1
    assert m["suppressed"]["suppressed_total"] == 2
    assert m["database"]["db_records"] == 4
    assert m["database"]["duplicate_db_vins"] == 1
    assert m["database"]["missing_evidence_with_frames"] == 1
    assert m["database"]["uv_conflicts"] >= 1
    assert m["ocr_latency_avg_ms"] is not None

    generate_reports(m, out, completed=False, duration_hours=24.0, elapsed_hours=0.5)
    for name in ("PRODUCTION_24H_AUDIT.md", "PRODUCTION_24H_METRICS.json",
                 "PRODUCTION_24H_PLC_EDGES.csv", "PRODUCTION_24H_SESSIONS.csv",
                 "PRODUCTION_24H_OCR_CONFLICTS.csv"):
        assert (out / name).is_file(), name
    audit_md = (out / "PRODUCTION_24H_AUDIT.md").read_text(encoding="utf-8")
    assert "NOT COMPLETED" in audit_md


def test_report_marks_completed(tmp_path):
    audit = tmp_path / "audit"; audit.mkdir()
    db = tmp_path / "ai_cam.db"
    m = collect_metrics(audit, db, tmp_path / "c")
    out = tmp_path / "r"
    generate_reports(m, out, completed=True, duration_hours=24.0, elapsed_hours=24.0)
    md = (out / "PRODUCTION_24H_AUDIT.md").read_text(encoding="utf-8")
    assert "PRODUCTION_24H_AUDIT: COMPLETED" in md
    assert "NOT COMPLETED" not in md
