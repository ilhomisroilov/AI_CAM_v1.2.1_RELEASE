# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

os.environ.setdefault("YOLO_AUTOINSTALL", "false")
os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")

root = Path(SPEC).resolve().parent

datas = [
    (str(root / "frontend"), "frontend"),
    (str(root / "config"), "config"),
    (str(root / "models"), "models"),
    (str(root / "migrations"), "migrations"),
    (str(root / "VERSION"), "."),
    (str(root / "README.md"), "."),
    (str(root / "PROPRIETARY_NOTICE.md"), "."),
]
# Ship only empty runtime-directory sentinels. Never copy a developer DB, log,
# collected crop, or dry-run cache into the executable distribution.
for runtime_dir in ("data", "logs", "crops", "temp", "engraved_ocr_collection"):
    datas.append(
        (str(root / "runtime" / runtime_dir / ".gitkeep"), f"runtime/{runtime_dir}")
    )
binaries = []
hiddenimports = [
    "backend.server",
    "backend.pipeline",
    "backend.ai.ocr_process",
    "backend.ai.engines.engraved_v121",
    "backend.ai.engines.paddle_profiles",
    "backend.database.ocr_v121_db",
    "ultralytics",
    "ultralytics.nn.tasks",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

for package in (
    "paddle",
    "paddleocr",
    "Cython",
    "onnx",
    "onnxruntime",
    "torch",
    "torchvision",
    "cv2",
    "fastapi",
    "uvicorn",
):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_hidden
    except Exception as exc:
        print(f"[spec] collect_all({package}) warning: {exc}")

# Do not collect/import every optional Ultralytics module here. Its tracker
# extras perform a package auto-install when imported and are not used by
# AI_CAM inference. The maintained PyInstaller hook supplies Ultralytics data
# files and the application imports the YOLO inference path statically.

a = Analysis(
    [str(root / "run.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(root / "tools" / "pyinstaller_runtime_hook.py")],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AI_CAM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory=".",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AI_CAM_v1.2.1",
)
