"""Windows DLL discovery for the CUDA runtime used by CTranslate2."""
from __future__ import annotations

import os
import sys
from ctypes import WinDLL
from pathlib import Path

from app.services import diagnostics

_dll_handles: list[object] = []
_added_directories: set[str] = set()
_last_probe_outcome: str | None = None


def _log_probe(outcome: str) -> None:
    """Record a concise CUDA outcome only when it changes.

    The diagnostic report must help distinguish a detected GPU from a usable
    transcription runtime, without recording user paths or media details.
    """
    global _last_probe_outcome
    if outcome == _last_probe_outcome:
        return
    _last_probe_outcome = outcome
    diagnostics.logger().info("CUDA runtime probe: %s", outcome)


def add_cuda_dll_directories() -> list[Path]:
    if sys.platform != "win32":
        return []
    roots = []
    for variable in ("CUDA_BIN_DIR", "CUDNN_BIN_DIR"):
        roots.extend(Path(value) for value in os.environ.get(variable, "").split(";") if value)
    toolkit_root = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA")
    if toolkit_root.is_dir():
        roots.extend(sorted(toolkit_root.glob("v12*"), reverse=True))
    added: list[Path] = []
    for item in roots:
        # ``CUDNN_BIN_DIR`` can point at a nested folder such as
        # ``...\bin\12.x\x64`` rather than a folder literally named ``bin``.
        # Treat an explicit directory containing a CUDA/cuDNN DLL as final.
        has_runtime_dll = (item / "cublas64_12.dll").is_file() or (item / "cudnn64_9.dll").is_file()
        folder = item if item.name.lower() == "bin" or has_runtime_dll else item / "bin"
        key = str(folder).lower()
        if folder.is_dir() and key not in _added_directories:
            _dll_handles.append(os.add_dll_directory(str(folder)))
            _added_directories.add(key)
            added.append(folder)
    return added


def cuda12_runtime_error() -> str | None:
    if sys.platform != "win32":
        return None
    added_directories = add_cuda_dll_directories()
    stage = "loading CUDA cuBLAS DLL"
    try:
        WinDLL("cublas64_12.dll")
        stage = "loading cuDNN DLL"
        WinDLL("cudnn64_9.dll")
        # The DLLs may be present while the actual CTranslate2 backend still
        # cannot initialise CUDA (for example after an incomplete cuDNN copy).
        # Ask the same runtime used by Whisper before claiming GPU readiness.
        stage = "importing CTranslate2"
        import ctranslate2

        stage = "checking the CUDA device in CTranslate2"
        device_count = ctranslate2.get_cuda_device_count()
        if device_count < 1:
            error = "CTranslate2 cannot access a CUDA device. Reinstall the NVIDIA GPU add-on and restart ClipFinder."
            _log_probe(f"failed while {stage}; CUDA/cuDNN DLLs loaded, CTranslate2 devices=0; {error}")
            return error
    except (OSError, ImportError, RuntimeError) as exc:
        detail = str(exc).strip()
        if detail:
            error = f"CUDA 12/cuDNN 9 could not be initialised: {detail}"
        else:
            error = "CUDA 12 cuBLAS and cuDNN 9 are not available. Install CUDA 12.x and place cuDNN 9 DLL files in its bin folder."
        diagnostics.log_failure(f"CUDA runtime probe failed while {stage}", exc)
        _log_probe(f"failed while {stage}; {diagnostics.redact(error)}")
        return error
    _log_probe(f"ready; CUDA DLL loaded, cuDNN DLL loaded, CTranslate2 devices={device_count}, new DLL directories={len(added_directories)}")
    return None
