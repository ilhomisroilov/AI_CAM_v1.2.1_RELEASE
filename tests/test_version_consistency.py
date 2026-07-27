from __future__ import annotations

from pathlib import Path

from backend.version import API_VERSION, APP_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_version_file_is_the_backend_source_of_truth() -> None:
    expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip().removeprefix("v")
    assert API_VERSION == expected
    assert APP_VERSION == f"v{expected}"


def test_frontend_has_no_independent_release_literal() -> None:
    frontend = ROOT / "frontend"
    for path in frontend.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".html", ".js", ".css"}:
            assert "v1.1.2" not in path.read_text(encoding="utf-8"), path


def test_runtime_entrypoints_use_the_shared_version_module() -> None:
    server = (ROOT / "backend" / "server.py").read_text(encoding="utf-8")
    runner = (ROOT / "run.py").read_text(encoding="utf-8")
    assert "from .version import API_VERSION" in server
    assert "version=API_VERSION" in server
    assert "from backend.version import APP_VERSION" in runner
