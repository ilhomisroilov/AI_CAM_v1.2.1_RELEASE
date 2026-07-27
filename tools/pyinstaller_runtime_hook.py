"""Early frozen-runtime policy needed before Paddle/Ultralytics imports."""

from __future__ import annotations

import os
from pathlib import Path
import site
import sys


os.environ.setdefault("YOLO_AUTOINSTALL", "false")
os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")

if getattr(sys, "frozen", False):
    release_root = Path(sys.executable).resolve().parent
    # Paddle 2.6's Windows bootstrap assumes site.USER_SITE is a string and
    # searches <USER_SITE>/paddle/libs. PyInstaller intentionally sets it to
    # None, so point it at the ONEDIR root containing the bundled paddle tree.
    site.USER_SITE = str(release_root)
