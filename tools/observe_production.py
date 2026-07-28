"""
============================================================
observe_production.py — 24h production observation (v1.3.0)
============================================================
Runs the REAL system (no mock) and, over an observation window, aggregates the
forensic artifacts it already writes — PLC edge audit, suppressed triggers, the
production database, OCR engine evidence, and the dataset collector manifests —
into a fixed set of reports.

Usage:
    python tools/observe_production.py --duration-hours 24
    python tools/observe_production.py --duration-hours 24 --launch   # also start run.py
    python run.py --observe-hours 24                                   # integrated

If the window does not complete (Ctrl-C, crash, or --duration-hours not elapsed),
the audit is written with status NOT COMPLETED. A 24h run is never faked.

Outputs (reports/):
    PRODUCTION_24H_AUDIT.md
    PRODUCTION_24H_METRICS.json
    PRODUCTION_24H_SESSIONS.csv
    PRODUCTION_24H_PLC_EDGES.csv
    PRODUCTION_24H_OCR_CONFLICTS.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

CONFUSABLE = {("U", "V"), ("V", "U"), ("E", "F"), ("F", "E")}


def _read_csv_rows(path: Path):
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _read_jsonl(path: Path):
    out = []
    if not path.is_file():
        return out
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _glob_rows(audit_dir: Path, stem: str, reader):
    rows = []
    for p in sorted(audit_dir.glob(f"{stem}_*.csv" if reader is _read_csv_rows else f"{stem}_*.jsonl")):
        rows.extend(reader(p))
    return rows


def collect_plc_metrics(audit_dir: Path) -> dict:
    rows = _glob_rows(audit_dir, "plc_edges", _read_csv_rows)
    rising = [r for r in rows if r.get("edge_type") == "rising"]
    intervals = []
    for r in rising:
        v = r.get("time_since_prev_rising_ms") or ""
        try:
            if v:
                intervals.append(float(v) / 1000.0)
        except Exception:
            pass
    dec = lambda name: sum(1 for r in rows if r.get("decision") == name)
    return {
        "total_rising_edges": len(rising),
        "accepted_rising_edges": dec("ACCEPTED"),
        "rejected_early_edges": dec("SUSPICIOUS_EARLY_TRIGGER"),
        "duplicate_ignored_edges": dec("DUPLICATE_TRIGGER_IGNORED"),
        "invalid_pulse_widths": dec("INVALID_PLC_PULSE"),
        "min_interval_sec": round(min(intervals), 2) if intervals else None,
        "max_interval_sec": round(max(intervals), 2) if intervals else None,
        "median_interval_sec": round(statistics.median(intervals), 2) if intervals else None,
        "_rows": rows,
    }


def collect_suppressed(audit_dir: Path) -> dict:
    rows = _glob_rows(audit_dir, "suppressed_triggers", _read_jsonl)
    return {
        "suppressed_total": len(rows),
        "suppressed_duplicate": sum(1 for r in rows if r.get("decision") == "DUPLICATE_TRIGGER_IGNORED"),
        "suppressed_early": sum(1 for r in rows if r.get("decision") == "SUSPICIOUS_EARLY_TRIGGER"),
    }


def collect_db_metrics(db_path: Path) -> dict:
    m = {"db_records": 0, "sessions": [], "status_counts": {},
         "missing_evidence_with_frames": 0, "duplicate_db_vins": 0,
         "ocr_latency_ms": [], "uv_conflicts": 0, "fe_conflicts": 0, "engine_rows": 0}
    if not db_path.is_file():
        return m
    try:
        conn = sqlite3.connect(str(db_path)); conn.row_factory = sqlite3.Row
    except Exception:
        return m
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM vin_records ORDER BY id")
            recs = [dict(r) for r in cur.fetchall()]
        except Exception:
            recs = []
        m["db_records"] = len(recs)
        seen_vin = {}
        for r in recs:
            st = r.get("status") or "?"
            m["status_counts"][st] = m["status_counts"].get(st, 0) + 1
            m["sessions"].append(r)
            vin = r.get("detected_vin") or r.get("vin")
            if vin and vin not in ("NO_READ", "NO_TAG"):
                if vin in seen_vin:
                    m["duplicate_db_vins"] += 1
                seen_vin[vin] = True
            # evidence path present?
            img = (r.get("image_path") or "").strip()
            if not img and st in ("NO_READ", "TIMEOUT", "OCR_AMBIGUOUS"):
                m["missing_evidence_with_frames"] += 1
        # engine evidence (optional table)
        try:
            cur.execute("SELECT session_id, engine_name, gated_text, latency_ms FROM ocr_engine_results")
            eng = [dict(r) for r in cur.fetchall()]
            m["engine_rows"] = len(eng)
            by_sess = {}
            for e in eng:
                if e.get("latency_ms") is not None:
                    try:
                        m["ocr_latency_ms"].append(float(e["latency_ms"]))
                    except Exception:
                        pass
                by_sess.setdefault(e["session_id"], []).append(e.get("gated_text") or "")
            for sid, texts in by_sess.items():
                texts = [t for t in texts if t]
                for i in range(max((len(t) for t in texts), default=0)):
                    chars = {t[i] for t in texts if i < len(t)}
                    for a in chars:
                        for b in chars:
                            if (a, b) in CONFUSABLE:
                                if {a, b} == {"U", "V"}:
                                    m["uv_conflicts"] += 1
                                elif {a, b} == {"E", "F"}:
                                    m["fe_conflicts"] += 1
        except Exception:
            pass
    finally:
        conn.close()
    return m


def collect_dataset_metrics(collection_root: Path) -> dict:
    total = eligible = equal_split = 0
    if collection_root.is_dir():
        for md in collection_root.rglob("metadata.json"):
            try:
                d = json.loads(md.read_text(encoding="utf-8"))
            except Exception:
                continue
            total += 1
            if d.get("training_eligible"):
                eligible += 1
            if str(d.get("alignment_source", "")).startswith("EQUAL_SPLIT"):
                equal_split += 1
    return {"dataset_samples": total, "dataset_training_eligible": eligible,
            "dataset_equal_split_samples": equal_split}


def collect_metrics(audit_dir: Path, db_path: Path, collection_root: Path) -> dict:
    plc = collect_plc_metrics(audit_dir)
    plc_rows = plc.pop("_rows")
    supp = collect_suppressed(audit_dir)
    db = collect_db_metrics(db_path)
    ds = collect_dataset_metrics(collection_root)
    lat = db.get("ocr_latency_ms", [])
    lat_sorted = sorted(lat)
    p95 = lat_sorted[int(0.95 * (len(lat_sorted) - 1))] if lat_sorted else None
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plc": plc, "suppressed": supp, "database": {k: v for k, v in db.items()
                                                      if k not in ("sessions", "ocr_latency_ms")},
        "dataset": ds,
        "ocr_latency_avg_ms": round(sum(lat) / len(lat), 1) if lat else None,
        "ocr_latency_p95_ms": round(p95, 1) if p95 is not None else None,
        "_plc_rows": plc_rows, "_sessions": db.get("sessions", []),
    }


def generate_reports(metrics: dict, out_dir: Path, completed: bool,
                     duration_hours: float, elapsed_hours: float,
                     metadata: dict = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    plc_rows = metrics.pop("_plc_rows", [])
    sessions = metrics.pop("_sessions", [])
    status = "COMPLETED" if completed else "NOT COMPLETED"
    if metadata is not None:
        metrics["run_metadata"] = metadata

    (out_dir / "PRODUCTION_24H_METRICS.json").write_text(
        json.dumps(metrics, indent=2, default=str), encoding="utf-8")

    # PLC edges CSV
    if plc_rows:
        keys = list(plc_rows[0].keys())
        with (out_dir / "PRODUCTION_24H_PLC_EDGES.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(plc_rows)
    # sessions CSV
    if sessions:
        keys = sorted({k for s in sessions for k in s.keys()})
        with (out_dir / "PRODUCTION_24H_SESSIONS.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(sessions)
    # OCR conflicts CSV
    db = metrics["database"]
    with (out_dir / "PRODUCTION_24H_OCR_CONFLICTS.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["conflict_type", "count"])
        w.writerow(["U/V", db.get("uv_conflicts", 0)])
        w.writerow(["F/E", db.get("fe_conflicts", 0)])

    plc = metrics["plc"]; supp = metrics["suppressed"]; ds = metrics["dataset"]
    meta = metadata or {}
    build_line = ""
    if meta:
        build_line = (f"\n**Build (frozen for the window):** version "
                      f"`{meta.get('version')}` · commit `{(meta.get('git_commit') or '')[:12]}` "
                      f"· config `{(meta.get('config_hash') or '')[:16]}` · "
                      f"git_dirty={meta.get('git_dirty')}\n")
    md = f"""# AI_CAM v1.2.1 Production 24h Audit

**Status: PRODUCTION_24H_AUDIT: {status}** — observed {elapsed_hours:.2f} h of a
{duration_hours:.0f} h window. Generated {metrics['generated_at']}.
{build_line}
## PLC / triggers
| metric | value |
|---|---|
| total raw rising edges | {plc['total_rising_edges']} |
| accepted rising edges | {plc['accepted_rising_edges']} |
| rejected early (SUSPICIOUS_EARLY_TRIGGER) | {plc['rejected_early_edges']} |
| duplicate ignored (active cycle) | {plc['duplicate_ignored_edges']} |
| invalid pulse widths | {plc['invalid_pulse_widths']} |
| interval min / median / max (s) | {plc['min_interval_sec']} / {plc['median_interval_sec']} / {plc['max_interval_sec']} |
| suppressed triggers (total) | {supp['suppressed_total']} |

## Database / body cycles
| metric | value |
|---|---|
| DB production records | {db['db_records']} |
| duplicate DB VINs | {db['duplicate_db_vins']} |
| status breakdown | {db['status_counts']} |
| records missing evidence image | {db['missing_evidence_with_frames']} |
| OCR engine evidence rows | {db['engine_rows']} |

## OCR conflicts / latency
| metric | value |
|---|---|
| U/V conflicts | {db['uv_conflicts']} |
| F/E conflicts | {db['fe_conflicts']} |
| OCR latency avg / p95 (ms) | {metrics['ocr_latency_avg_ms']} / {metrics['ocr_latency_p95_ms']} |

## Dataset
| metric | value |
|---|---|
| dataset samples | {ds['dataset_samples']} |
| training-eligible | {ds['dataset_training_eligible']} |
| equal-split samples | {ds['dataset_equal_split_samples']} |

"""
    if not completed:
        md += ("\n> **PRODUCTION_24H_AUDIT: NOT COMPLETED** — the full observation window "
               "did not elapse. Re-run `python tools/observe_production.py --duration-hours 24` "
               "on the production host and do not declare FINAL v1.3.0 until it completes.\n")
    (out_dir / "PRODUCTION_24H_AUDIT.md").write_text(md, encoding="utf-8")


def observe(duration_hours: float, audit_dir: Path, db_path: Path,
            collection_root: Path, out_dir: Path, launch: bool,
            poll_sec: float = 60.0) -> int:
    proc: Optional[subprocess.Popen] = None
    if launch:
        proc = subprocess.Popen([sys.executable, str(ROOT / "run.py")], cwd=str(ROOT))
    start = time.monotonic()
    deadline = start + duration_hours * 3600.0
    completed = False
    try:
        while time.monotonic() < deadline:
            time.sleep(min(poll_sec, max(1.0, deadline - time.monotonic())))
        completed = True
    except KeyboardInterrupt:
        completed = False
    finally:
        elapsed_h = (time.monotonic() - start) / 3600.0
        metrics = collect_metrics(audit_dir, db_path, collection_root)
        generate_reports(metrics, out_dir, completed, duration_hours, elapsed_h,
                         metadata=run_metadata())
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
    print(f"[OBSERVE] {'COMPLETED' if completed else 'NOT COMPLETED'} "
          f"({elapsed_h:.2f}/{duration_hours:.0f}h) -> {out_dir}")
    return 0 if completed else 3


def run_metadata() -> dict:
    """Freeze identity of the observed build: version, git commit, config + model
    hashes — so the 24h report proves code/config did not change mid-observation."""
    import hashlib
    md = {"version": None, "git_commit": None, "git_dirty": None,
          "config_hash": None, "model_hashes": {}}
    try:
        from backend.version import API_VERSION
        md["version"] = API_VERSION
    except Exception:
        pass
    try:
        md["git_commit"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                                           capture_output=True, text=True, timeout=10).stdout.strip()
        md["git_dirty"] = bool(subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                                              capture_output=True, text=True, timeout=10).stdout.strip())
    except Exception:
        pass

    def _sha(p: Path):
        try:
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None
    try:
        from backend.config import (BASE_DIR, MODELS_DIR, TRAINED_MODEL_PATH,
                                     PADDLE_DET_DIR, PADDLE_REC_DIR, PADDLE_CLS_DIR)
        md["config_hash"] = _sha(BASE_DIR / "config" / "settings.yaml")
        md["model_hashes"] = {
            "yolo": _sha(TRAINED_MODEL_PATH),
            "engraved_onnx": _sha(MODELS_DIR / "engraved_ocr_v1.2.1" / "model.onnx"),
            "paddle_det": _sha(PADDLE_DET_DIR / "inference.pdmodel"),
            "paddle_rec": _sha(PADDLE_REC_DIR / "inference.pdmodel"),
            "paddle_cls": _sha(PADDLE_CLS_DIR / "inference.pdmodel"),
        }
    except Exception:
        pass
    return md


def _default_paths():
    try:
        from backend.config import RUNTIME_DIR, DB_PATH, OCR_RELEASE, BASE_DIR
        audit = Path(RUNTIME_DIR) / "audit"
        coll = Path(BASE_DIR) / getattr(OCR_RELEASE, "collection_root", "runtime/engraved_ocr_collection")
        return audit, Path(DB_PATH), coll, Path(BASE_DIR) / "reports"
    except Exception:
        return (ROOT / "runtime" / "audit", ROOT / "runtime" / "data" / "ai_cam.db",
                ROOT / "runtime" / "engraved_ocr_collection", ROOT / "reports")


def main(argv=None) -> int:
    a, d, c, o = _default_paths()
    ap = argparse.ArgumentParser(description="AI_CAM 24h production observation")
    ap.add_argument("--duration-hours", type=float, default=24.0)
    ap.add_argument("--audit-dir", default=str(a))
    ap.add_argument("--db", default=str(d))
    ap.add_argument("--collection-root", default=str(c))
    ap.add_argument("--out", default=str(o))
    ap.add_argument("--launch", action="store_true", help="also start run.py for the window")
    ap.add_argument("--report-only", action="store_true", help="generate reports now, no wait")
    args = ap.parse_args(argv)
    if args.report_only:
        metrics = collect_metrics(Path(args.audit_dir), Path(args.db), Path(args.collection_root))
        generate_reports(metrics, Path(args.out), completed=False,
                         duration_hours=args.duration_hours, elapsed_hours=0.0,
                         metadata=run_metadata())
        print(f"[OBSERVE] report-only -> {args.out}")
        return 0
    return observe(args.duration_hours, Path(args.audit_dir), Path(args.db),
                   Path(args.collection_root), Path(args.out), args.launch)


if __name__ == "__main__":
    raise SystemExit(main())
