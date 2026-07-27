"""Load the production PaddleOCR worker once and exit non-zero on failure.

This intentionally uses the same process-isolated OCR pool as the server. It
therefore catches model-download, native-library, CUDA and PaddleOCR init
failures which a framework-only tensor probe cannot see.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="AI_CAM PaddleOCR preload smoke test")
    parser.add_argument("--profile", choices=("cpu", "gpu"), required=True)
    args = parser.parse_args()

    from backend.config import OCR
    from backend.ai.ocr_worker import OCRWorker

    OCR.use_gpu = args.profile == "gpu"
    worker = OCRWorker(on_result=lambda *_args: None)
    ready = False
    try:
        ready = bool(worker.preload())
    finally:
        worker.shutdown()

    print(json.dumps({
        "profile": args.profile,
        "ocr_preload_ready": ready,
        "paddleocr_process_isolated": True,
    }, sort_keys=True))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
