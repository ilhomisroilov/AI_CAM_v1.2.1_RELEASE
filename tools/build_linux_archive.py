"""Build the secret-free AI_CAM Ubuntu source/model deployment archive."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_NAME = "AI_CAM_v1.2.1_ubuntu26_amd64.tar.gz"
FORBIDDEN_PARTS = {
    ".git", ".venv", ".idea", ".pytest_cache", "__pycache__",
    "runtime", "build", "dist", "dist-linux",
}
FORBIDDEN_PATHS = {
    "deploy/local/ai-cam.env",
    "ai-cam.env",
    ".env",
}
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    paths = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        posix = relative.as_posix()
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            continue
        if posix in FORBIDDEN_PATHS or posix.startswith("deploy/local/"):
            continue
        source = ROOT / relative
        if source.is_file():
            paths.append(relative)
    return sorted(paths, key=lambda item: item.as_posix())


def _verify_models(paths: list[Path]) -> None:
    required = {
        Path("models/yolo/best.pt"),
        Path("models/engraved_ocr_v1.2.1/model.onnx"),
        Path("models/paddle/det/inference.pdmodel"),
        Path("models/paddle/det/inference.pdiparams"),
        Path("models/paddle/rec/inference.pdmodel"),
        Path("models/paddle/rec/inference.pdiparams"),
        Path("models/paddle/cls/inference.pdmodel"),
        Path("models/paddle/cls/inference.pdiparams"),
    }
    missing = sorted(str(path) for path in required - set(paths))
    if missing:
        raise RuntimeError(f"required model artifacts are not tracked: {missing}")
    for relative in paths:
        if relative.parts and relative.parts[0] == "models":
            source = ROOT / relative
            with source.open("rb") as stream:
                prefix = stream.read(len(LFS_POINTER))
            if prefix == LFS_POINTER:
                raise RuntimeError(
                    f"Git LFS content is not materialized: {relative}"
                )
            if source.stat().st_size <= 0:
                raise RuntimeError(f"empty model artifact: {relative}")


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    if info.isdir():
        info.mode = 0o755
    elif info.name.endswith(".sh"):
        info.mode = 0o755
    else:
        info.mode = 0o644
    return info


def build(output_dir: Path) -> tuple[Path, str]:
    if _git("status", "--porcelain"):
        raise RuntimeError("archive build requires a clean committed worktree")
    branch = _git("branch", "--show-current")
    commit = _git("rev-parse", "HEAD")
    start_commit = "86e9028a879d757e217506df139e486b065cc34b"
    paths = _tracked_files()
    _verify_models(paths)

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / ARCHIVE_NAME
    checksums = output_dir / "SHA256SUMS"
    epoch = int(_git("show", "-s", "--format=%ct", "HEAD"))
    os.environ.setdefault("SOURCE_DATE_EPOCH", str(epoch))

    with tempfile.TemporaryDirectory(prefix="ai-cam-linux-package-") as temp_name:
        stage = Path(temp_name)
        manifest_lines = []
        for relative in paths:
            source = ROOT / relative
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            manifest_lines.append(f"{_sha256(target)}  {relative.as_posix()}")

        provenance = (
            "AI_CAM Ubuntu deployment provenance\n"
            "====================================\n"
            "version: 1.2.1\n"
            f"branch: {branch}\n"
            f"start_commit: {start_commit}\n"
            f"source_commit: {commit}\n"
            "target: Ubuntu Server 26.04 LTS amd64\n"
            "python_contract: CPython 3.11\n"
            f"source_date_epoch: {epoch}\n"
            "live_hardware_validation: not performed by this archive build\n"
        )
        (stage / "PROVENANCE.txt").write_text(provenance, encoding="utf-8")
        manifest_lines.append(
            f"{_sha256(stage / 'PROVENANCE.txt')}  PROVENANCE.txt"
        )
        (stage / "MANIFEST.sha256").write_text(
            "\n".join(sorted(manifest_lines)) + "\n", encoding="utf-8"
        )

        temporary_archive = output_dir / f".{ARCHIVE_NAME}.tmp"
        with temporary_archive.open("wb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, mtime=epoch
            ) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as tar:
                    for path in sorted(
                        stage.rglob("*"), key=lambda item: item.relative_to(stage).as_posix()
                    ):
                        tar.add(
                            path,
                            arcname=path.relative_to(stage).as_posix(),
                            recursive=False,
                            filter=_tar_filter,
                        )
        temporary_archive.replace(archive)

    archive_hash = _sha256(archive)
    checksums.write_text(
        f"{archive_hash}  {ARCHIVE_NAME}\n", encoding="ascii"
    )
    return archive, archive_hash


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist-linux",
    )
    args = parser.parse_args()
    archive, digest = build(args.output_dir)
    print(f"archive={archive}")
    print(f"sha256={digest}")
    print(f"bytes={archive.stat().st_size}")
    print(f"built_at={time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
