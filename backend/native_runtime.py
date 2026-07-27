"""Bootstrap native CUDA library lookup for isolated AI_CAM subprocesses.

PyTorch CUDA wheels keep CUDA libraries below ``site-packages/nvidia/*/lib``.
Paddle's cuDNN-in wheel bundles cuDNN 8, but that cuDNN still needs CUDA 11
libraries such as ``libcublas.so.11``.  The ELF loader does not use PyTorch's
private RUNPATH for this transitive Paddle dependency, so spawned Paddle/OCR
processes need those non-cuDNN directories in ``LD_LIBRARY_PATH``.

The PyTorch ``nvidia/cudnn/lib`` directory is deliberately excluded: Torch
2.4.1 carries cuDNN 9 while Paddle 2.6.1 uses its bundled cuDNN 8.  Exposing
cuDNN 9 globally would recreate the ABI collision this bootstrap prevents.
"""
from __future__ import annotations

import os
import platform
import site
import sys
import sysconfig
from pathlib import Path
from typing import Iterable, Mapping, MutableMapping, Optional


_COMPAT_DIR_NAME = "ai_cam_native_libs"


def _python_search_roots() -> list[Path]:
    values: list[str] = [entry for entry in sys.path if entry]
    try:
        values.extend(site.getsitepackages())
    except (AttributeError, OSError):
        pass
    try:
        user_site = site.getusersitepackages()
        if isinstance(user_site, str):
            values.append(user_site)
    except (AttributeError, OSError):
        pass
    values.extend(
        str(value) for value in sysconfig.get_paths().values() if value
    )

    roots: list[Path] = []
    seen: set[str] = set()
    for value in values:
        try:
            path = Path(value).resolve()
        except (OSError, RuntimeError):
            continue
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            roots.append(path)
    return roots


def discover_nvidia_library_dirs(
    search_roots: Optional[Iterable[Path | str]] = None,
) -> list[Path]:
    """Find pip-installed CUDA library dirs, excluding incompatible cuDNN."""
    roots = (
        [Path(root) for root in search_roots]
        if search_roots is not None
        else _python_search_roots()
    )
    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        nvidia_root = root / "nvidia"
        if not nvidia_root.is_dir():
            continue
        try:
            components = sorted(nvidia_root.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for component in components:
            # Paddle cudnn-in supplies cuDNN 8; Torch 2.4.1's cuDNN 9 must not
            # become the global dependency selected for Paddle.
            if component.name.lower() == "cudnn":
                continue
            lib_dir = component / "lib"
            try:
                has_shared_library = lib_dir.is_dir() and any(
                    lib_dir.glob("lib*.so*")
                )
            except OSError:
                has_shared_library = False
            if not has_shared_library:
                continue
            try:
                resolved = lib_dir.resolve()
            except (OSError, RuntimeError):
                resolved = lib_dir
            key = os.path.normcase(str(resolved))
            if key not in seen:
                seen.add(key)
                found.append(resolved)
    return found


def _compatibility_link_dir() -> Path:
    """Return an isolated venv-owned directory for unversioned ELF aliases."""
    return Path(sys.prefix) / _COMPAT_DIR_NAME


def find_versioned_cuda_library(
    filename: str,
    library_dirs: Optional[Iterable[Path | str]] = None,
) -> Optional[Path]:
    """Find the newest versioned implementation for an unversioned SONAME."""
    candidates: list[Path] = []
    directories = (
        [Path(path) for path in library_dirs]
        if library_dirs is not None
        else discover_nvidia_library_dirs()
    )
    for directory in directories:
        try:
            candidates.extend(
                path for path in directory.glob(filename + ".*") if path.is_file()
            )
        except OSError:
            continue
    return sorted(candidates, key=lambda path: path.name)[-1] if candidates else None


def cuda_compatibility_alias_targets(
    library_dirs: Optional[Iterable[Path | str]] = None,
) -> dict[str, Path]:
    """Map unversioned CUDA loader names to pip-wheel versioned libraries."""
    directories = (
        [Path(path) for path in library_dirs]
        if library_dirs is not None
        else discover_nvidia_library_dirs()
    )
    candidates: dict[str, list[Path]] = {}
    for directory in directories:
        try:
            versioned = [path for path in directory.glob("lib*.so.*") if path.is_file()]
        except OSError:
            continue
        for target in versioned:
            stem, separator, _version = target.name.partition(".so.")
            if not separator:
                continue
            alias = stem + ".so"
            # Paddle's cudnn-in wheel owns cuDNN 8. Never expose Torch cuDNN 9.
            if alias.lower().startswith("libcudnn"):
                continue
            candidates.setdefault(alias, []).append(target)
    return {
        alias: sorted(paths, key=lambda path: path.name)[-1]
        for alias, paths in sorted(candidates.items())
    }


def ensure_cuda_compatibility_links(
    link_dir: Optional[Path | str] = None,
    library_dirs: Optional[Iterable[Path | str]] = None,
) -> list[Path]:
    """Create venv-local aliases required by Paddle's bundled cuDNN 8.

    NVIDIA's pip wheels contain versioned libraries such as
    ``libnvrtc.so.11.2`` and ``libcublas.so.11`` but omit the unversioned names
    that Paddle loads dynamically. Keep aliases outside installed packages so
    pip remains authoritative and upgrades are recoverable.
    """
    if platform.system() != "Linux":
        return []
    destination_dir = Path(link_dir) if link_dir is not None else _compatibility_link_dir()
    directories = (
        [Path(path) for path in library_dirs]
        if library_dirs is not None
        else discover_nvidia_library_dirs()
    )
    created: list[Path] = []
    for filename, target in cuda_compatibility_alias_targets(directories).items():
        destination_dir.mkdir(parents=True, exist_ok=True)
        alias = destination_dir / filename
        try:
            if alias.is_symlink() and alias.resolve() == target.resolve():
                created.append(alias)
                continue
            if alias.exists() or alias.is_symlink():
                alias.unlink()
            alias.symlink_to(target.resolve())
        except OSError as exc:
            raise RuntimeError(
                f"Cannot create native CUDA compatibility alias {filename}: {exc}"
            ) from exc
        created.append(alias)
    return created


def native_library_dirs() -> list[Path]:
    """Return ordered child loader dirs, with compatibility aliases first."""
    directories: list[Path] = []
    compatibility = _compatibility_link_dir()
    try:
        if compatibility.is_dir() and any(compatibility.glob("lib*.so*")):
            directories.append(compatibility.resolve())
    except OSError:
        pass
    directories.extend(discover_nvidia_library_dirs())
    return directories


def native_subprocess_env(
    base: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Return an environment where Paddle children can resolve CUDA 11 libs."""
    env = dict(os.environ if base is None else base)
    if platform.system() != "Linux":
        return env

    discovered = [str(path) for path in native_library_dirs()]
    existing = [
        value for value in env.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if value
    ]
    ordered: list[str] = []
    seen: set[str] = set()
    for value in [*discovered, *existing]:
        key = os.path.normcase(value)
        if key not in seen:
            seen.add(key)
            ordered.append(value)
    if ordered:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(ordered)
    return env


def configure_native_library_path(
    environ: Optional[MutableMapping[str, str]] = None,
) -> list[Path]:
    """Prepare the environment inherited by all later Paddle/OCR children."""
    target = os.environ if environ is None else environ
    if platform.system() == "Linux":
        ensure_cuda_compatibility_links()
    updated = native_subprocess_env(target)
    if "LD_LIBRARY_PATH" in updated:
        target["LD_LIBRARY_PATH"] = updated["LD_LIBRARY_PATH"]
    return native_library_dirs() if platform.system() == "Linux" else []
