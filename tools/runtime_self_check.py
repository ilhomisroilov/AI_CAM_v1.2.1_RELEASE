"""Standalone wrapper around the AI_CAM production runtime self-check."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.runtime import collect_runtime_diagnostics, evaluate_runtime, report_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Check AI_CAM CPU/CUDA runtime")
    parser.add_argument("--deep", action="store_true", help="run real tensor operations")
    parser.add_argument("--require-gpu", action="store_true", help="fail unless both engines use CUDA")
    args = parser.parse_args()
    report = collect_runtime_diagnostics(deep=bool(args.deep or args.require_gpu))
    ok, failures = evaluate_runtime(report, require_gpu=args.require_gpu)
    report["check"] = {"ok": ok, "require_gpu": args.require_gpu, "failures": failures}
    print(report_json(report))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
