"""Pre-flight diagnostics for the source checkout.

CPU transcription is a supported base configuration.  NVIDIA and CUDA are
reported separately as an optional acceleration capability, never as a reason
to fail an otherwise usable ClipFinder installation.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from app.services.cuda_runtime import cuda12_runtime_error


def check(label: str, present: bool, detail: str = "") -> bool:
    mark = "OK" if present else "MISSING"
    print(f"[{mark}] {label}{': ' + detail if detail else ''}")
    return present


def main() -> int:
    base_ok = True
    base_ok &= check("Python 3.11 x64", sys.version_info[:2] == (3, 11) and sys.maxsize > 2**32, sys.version.split()[0])
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    base_ok &= check("FFmpeg", bool(ffmpeg), ffmpeg or "add ffmpeg to PATH")
    base_ok &= check("FFprobe", bool(ffprobe), ffprobe or "installed with FFmpeg")
    nvidia_smi = shutil.which("nvidia-smi")
    gpu_name = ""
    if nvidia_smi:
        result = subprocess.run([nvidia_smi, "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True)
        gpu_name = result.stdout.strip().replace("\n", "; ")
    check("NVIDIA GPU (optional)", bool(gpu_name), gpu_name or "not detected; CPU mode remains supported")
    if sys.platform == "win32" and gpu_name:
        runtime_error = cuda12_runtime_error()
        check(
            "GPU transcription runtime (optional)",
            runtime_error is None,
            "ready" if runtime_error is None else runtime_error,
        )
    if base_ok:
        print("\nBase environment is ready. GPU acceleration is optional.")
        return 0
    print("\nInstall the missing base components, then run this test again.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
