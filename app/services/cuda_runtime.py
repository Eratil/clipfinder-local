"""Windows DLL discovery for the CUDA runtime used by CTranslate2."""
from __future__ import annotations

import os
import sys
from ctypes import WinDLL
from pathlib import Path

_dll_handles: list[object] = []


def add_cuda_dll_directories() -> list[Path]:
    if sys.platform != "win32":
        return []
    roots = [Path(value) for value in os.environ.get("CUDA_BIN_DIR", "").split(";") if value]
    toolkit_root = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA")
    if toolkit_root.is_dir():
        roots.extend(sorted(toolkit_root.glob("v12*"), reverse=True))
    added: list[Path] = []
    for item in roots:
        folder = item if item.name.lower() == "bin" else item / "bin"
        if folder.is_dir():
            _dll_handles.append(os.add_dll_directory(str(folder)))
            added.append(folder)
    return added


def cuda12_runtime_error() -> str | None:
    if sys.platform != "win32":
        return None
    add_cuda_dll_directories()
    try:
        WinDLL("cublas64_12.dll")
        WinDLL("cudnn64_9.dll")
    except OSError:
        return "CUDA 12 cuBLAS and cuDNN 9 are not available. Install CUDA 12.x and place cuDNN 9 DLL files in its bin folder."
    return None
