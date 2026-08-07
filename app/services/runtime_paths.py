"""Standard-library-only discovery of compatible CUDA 12/cuDNN 9 pairs."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.services.model_catalog import runtime_compatibility


@dataclass(frozen=True)
class GpuRuntimePair:
    version: tuple[int, int]
    cuda_bin: Path
    cudnn_bin: Path
    cudnn_package_version: tuple[int, int] = (0, 0)


def _version_from_path(path: Path) -> tuple[int, int] | None:
    matches = re.findall(r"(?:^|[\\/])v?(\d+)\.(\d+)(?=[\\/]|$)", str(path), re.IGNORECASE)
    return (int(matches[-1][0]), int(matches[-1][1])) if matches else None


def _supported(version: tuple[int, int]) -> bool:
    cuda = runtime_compatibility()["cuda"]
    return version[0] == int(cuda["major"]) and int(cuda["minimum_minor"]) <= version[1] <= int(cuda["maximum_tested_minor"])


def _required_dlls(component: str) -> tuple[str, ...]:
    values = runtime_compatibility().get(component, {}).get("required_dlls", [])
    return tuple(str(value) for value in values if value)


def _complete(directory: Path, component: str) -> bool:
    required = _required_dlls(component)
    return directory.is_dir() and bool(required) and all((directory / name).is_file() for name in required)


def _cudnn_package_version(path: Path) -> tuple[int, int]:
    matches = re.findall(r"(?:^|[\\/])CUDNN[\\/]v(\d+)\.(\d+)(?=[\\/]|$)", str(path), re.IGNORECASE)
    return (int(matches[-1][0]), int(matches[-1][1])) if matches else (0, 0)


def _program_files() -> Path:
    return Path(os.environ.get("ProgramFiles") or r"C:\Program Files")


def compatible_runtime_pairs() -> list[GpuRuntimePair]:
    """Return complete, same-minor runtime pairs from newest to oldest."""
    explicit_cuda = [Path(value) for value in os.environ.get("CUDA_BIN_DIR", "").split(";") if value]
    explicit_cudnn = [Path(value) for value in os.environ.get("CUDNN_BIN_DIR", "").split(";") if value]
    explicit_pairs: list[GpuRuntimePair] = []
    for cuda_bin in explicit_cuda:
        cuda_version = _version_from_path(cuda_bin)
        # Do not guess a CUDA minor from an arbitrary custom directory. A
        # versioned path is required so cuDNN can be matched deterministically.
        if not cuda_version or not _supported(cuda_version) or not _complete(cuda_bin, "cuda"):
            continue
        for cudnn_bin in explicit_cudnn or [cuda_bin]:
            if not _complete(cudnn_bin, "cudnn"):
                continue
            cudnn_version = _version_from_path(cudnn_bin)
            if cudnn_version is None and cudnn_bin.resolve() == cuda_bin.resolve():
                cudnn_version = cuda_version
            if cuda_version == cudnn_version:
                explicit_pairs.append(
                    GpuRuntimePair(cuda_version, cuda_bin, cudnn_bin, _cudnn_package_version(cudnn_bin))
                )

    cuda_root = _program_files() / "NVIDIA GPU Computing Toolkit" / "CUDA"
    cudnn_root = _program_files() / "NVIDIA" / "CUDNN"
    cuda_bins: list[tuple[tuple[int, int], Path]] = []
    if cuda_root.is_dir():
        for directory in cuda_root.iterdir():
            version = _version_from_path(directory)
            binary = directory / "bin"
            if version and _supported(version) and _complete(binary, "cuda"):
                cuda_bins.append((version, binary))
    standalone_cudnn: dict[tuple[int, int], list[tuple[tuple[int, int], Path]]] = {}
    if cudnn_root.is_dir():
        first_cudnn_dll = _required_dlls("cudnn")[0]
        for library in cudnn_root.rglob(first_cudnn_dll):
            version = _version_from_path(library.parent)
            if version and _supported(version) and _complete(library.parent, "cudnn"):
                standalone_cudnn.setdefault(version, []).append(
                    (_cudnn_package_version(library.parent), library.parent)
                )
    discovered_pairs: list[GpuRuntimePair] = []
    for version, cuda_bin in cuda_bins:
        candidates: list[tuple[tuple[int, int], Path]] = []
        if _complete(cuda_bin, "cudnn"):
            candidates.append(((0, 0), cuda_bin))
        candidates.extend(standalone_cudnn.get(version, []))
        for package_version, cudnn_bin in sorted(candidates, key=lambda item: item[0], reverse=True):
            discovered_pairs.append(GpuRuntimePair(version, cuda_bin, cudnn_bin, package_version))

    unique: dict[tuple[str, str], GpuRuntimePair] = {}
    ordered = explicit_pairs + sorted(
        discovered_pairs,
        key=lambda pair: (pair.version, pair.cudnn_package_version),
        reverse=True,
    )
    for pair in ordered:
        unique.setdefault((str(pair.cuda_bin).lower(), str(pair.cudnn_bin).lower()), pair)
    return list(unique.values())


def preferred_runtime_pair() -> GpuRuntimePair | None:
    pairs = compatible_runtime_pairs()
    return pairs[0] if pairs else None
