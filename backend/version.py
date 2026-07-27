"""AI_CAM release version loaded from the repository-level VERSION file."""

from __future__ import annotations

from pathlib import Path


_VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"


def _read_version() -> str:
    try:
        version = _VERSION_FILE.read_text(encoding="utf-8").strip().removeprefix("v")
    except OSError as exc:  # A broken deployment must not silently invent a version.
        raise RuntimeError(f"AI_CAM VERSION fayli o'qilmadi: {_VERSION_FILE}") from exc
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise RuntimeError(f"AI_CAM VERSION noto'g'ri semver: {version!r}")
    return version


API_VERSION = _read_version()
APP_VERSION = f"v{API_VERSION}"
__version__ = API_VERSION
