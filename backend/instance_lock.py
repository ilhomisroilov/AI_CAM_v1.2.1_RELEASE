"""Portable single-instance guard for the canonical AI_CAM launcher."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .config import RUNTIME_DIR


class InstanceLockError(RuntimeError):
    """Raised when another launcher already owns the runtime lock."""


class InstanceLock:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or (RUNTIME_DIR / "ai-cam.lock")).resolve()
        self._handle = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="ascii")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if not handle.read(1):
                    handle.write(" ")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            owner = "unknown"
            try:
                owner = self.path.read_text(encoding="ascii").strip() or "unknown"
            except OSError:
                pass
            raise InstanceLockError(
                f"another AI_CAM instance owns {self.path} (recorded PID: {owner})"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()
