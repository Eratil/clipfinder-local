"""Pre-flight diagnostics; this script deliberately needs no third-party package."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from ctypes import WinDLL
from pathlib import Path

_dll_handles: list[object] = []


def check(label: str, present: bool, detail: str = "") -> bool:
    mark = "OK" if present else "MISSING"
    print(f"[{mark}] {label}{': ' + detail if detail else ''}")
    return present


def cuda12_bin_folders() -> list[Path]:
    root = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA")
    folders = sorted((path / "bin" for path in root.glob("v12*") if (path / "bin").is_dir()), reverse=True) if root.is_dir() else []
    for folder in folders:
        _dll_handles.append(os.add_dll_directory(str(folder)))
    return folders


def main() -> int:
    ok = True
    ok &= check("Python", sys.version_info >= (3, 11), sys.version.split()[0])
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    ok &= check("FFmpeg", bool(ffmpeg), ffmpeg or "add ffmpeg to PATH")
    ok &= check("FFprobe", bool(ffprobe), ffprobe or "installed with FFmpeg")
    nvidia_smi = shutil.which("nvidia-smi")
    gpu_name = ""
    if nvidia_smi:
        result = subprocess.run([nvidia_smi, "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True)
        gpu_name = result.stdout.strip().replace("\n", "; ")
    ok &= check("NVIDIA GPU", bool(gpu_name), gpu_name or "check NVIDIA driver")
    if sys.platform == "win32":
        folders = cuda12_bin_folders()
        check("CUDA 12 bin folder", bool(folders), str(folders[0]) if folders else "install CUDA 12.x alongside CUDA 13")
        for library, label in (("cublas64_12.dll", "CUDA 12 cuBLAS"), ("cudnn64_9.dll", "CUDA 12 cuDNN 9")):
            try:
                WinDLL(library)
                present = True
            except OSError:
                present = False
            ok &= check(label, present, "required for GPU transcription" if not present else "")
    if ok:
        print("\nBase environment is ready.")
        return 0
    print("\nInstall the missing components, then run this test again.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
