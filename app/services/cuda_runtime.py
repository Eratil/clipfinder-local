"""Windows DLL discovery for the CUDA runtime used by CTranslate2."""
from __future__ import annotations

import os
import sys
from ctypes import WinDLL
from pathlib import Path

from app.services import diagnostics
from app.services.model_catalog import runtime_compatibility
from app.services.runtime_paths import preferred_runtime_pair

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


def add_cuda_dll_directories(pair=None) -> list[Path]:
    if sys.platform != "win32":
        return []
    pair = pair or preferred_runtime_pair()
    roots = [pair.cuda_bin, pair.cudnn_bin] if pair else []
    added: list[Path] = []
    for item in roots:
        folder = item
        key = str(folder).lower()
        if folder.is_dir() and key not in _added_directories:
            _dll_handles.append(os.add_dll_directory(str(folder)))
            _added_directories.add(key)
            added.append(folder)
    return added


def cuda12_runtime_error() -> str | None:
    if sys.platform != "win32":
        return None
    added_directories: list[Path] = []
    stage = "discovering compatible CUDA and cuDNN directories"
    try:
        pair = preferred_runtime_pair()
        if pair is None:
            raise OSError("No complete, same-minor CUDA 12 and cuDNN 9 pair was found.")
        added_directories = add_cuda_dll_directories(pair)
        compatibility = runtime_compatibility()
        for component, directory in (("cuda", pair.cuda_bin), ("cudnn", pair.cudnn_bin)):
            for name in compatibility[component]["required_dlls"]:
                stage = f"loading {name}"
                WinDLL(str(directory / name))
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
