"""Verify the release's required imports and Paddle/protobuf compatibility."""

from __future__ import annotations

import importlib
import os

import google.protobuf

os.environ.setdefault("YOLO_AUTOINSTALL", "false")
os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")


REQUIRED_MODULES = (
    "torch",
    "torchvision",
    "ultralytics",
    "paddle",
    "paddleocr",
    "onnx",
    "onnxruntime",
    "cv2",
    "numpy",
    "PIL",
    "fastapi",
    "uvicorn",
    "sqlite3",
)


def main() -> int:
    failures: list[str] = []
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # pragma: no cover - diagnostic script
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    protobuf_version = google.protobuf.__version__
    if int(protobuf_version.split(".", 1)[0]) >= 4:
        failures.append(
            f"protobuf {protobuf_version}: Paddle-compatible major <4 required"
        )

    if failures:
        print("Import verification failed:\n  " + "\n  ".join(failures))
        return 1

    print(f"Import verification OK; protobuf={protobuf_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
