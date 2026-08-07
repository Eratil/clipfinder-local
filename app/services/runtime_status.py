"""Small, non-invasive runtime diagnostics for the desktop interface."""
from __future__ import annotations

import copy
import os
import subprocess
import threading
import time

from app.config import settings
from app.services.cuda_runtime import cuda12_runtime_error
from app.services.embeddings import current_device as embedding_device, installation_supports_cuda as embedding_installation_supports_cuda


_HARDWARE_CACHE_TTL_SECONDS = 300.0
_hardware_cache_lock = threading.Lock()
_hardware_cache: tuple[float, dict] | None = None


def _nvidia_gpu() -> dict | None:
    options: dict = {"capture_output": True, "text": True, "timeout": 3}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            **options,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode or not result.stdout.strip():
        return None
    name, memory_mb, driver = (part.strip() for part in result.stdout.splitlines()[0].split(",", 2))
    return {"name": name, "memory_mb": memory_mb, "driver": driver}


def _probe_hardware() -> dict:
    configured_cuda = settings.whisper_device.lower() == "cuda"
    cuda_error = cuda12_runtime_error() if configured_cuda else None
    gpu = _nvidia_gpu()
    # The CTranslate2 probe is authoritative. `nvidia-smi` is useful only for
    # displaying the card name/VRAM and is not guaranteed to be on PATH on an
    # otherwise correctly configured machine.
    gpu_ready = configured_cuda and not cuda_error
    return {
        "configured_cuda": configured_cuda,
        "cuda_error": cuda_error,
        "gpu": gpu,
        "gpu_ready": gpu_ready,
    }


def invalidate_runtime_status_cache() -> None:
    """Force the next status request to probe NVIDIA/CUDA again."""
    global _hardware_cache
    with _hardware_cache_lock:
        _hardware_cache = None


def _hardware_status() -> dict:
    """Cache slow driver and DLL probes while keeping model state live."""
    global _hardware_cache
    now = time.monotonic()
    with _hardware_cache_lock:
        if _hardware_cache and now - _hardware_cache[0] < _HARDWARE_CACHE_TTL_SECONDS:
            return copy.deepcopy(_hardware_cache[1])
        result = _probe_hardware()
        _hardware_cache = (now, result)
        return copy.deepcopy(result)


def runtime_status() -> dict:
    """Return what the app can really use now, without loading any ML model."""
    hardware = _hardware_status()
    configured_cuda = hardware["configured_cuda"]
    cuda_error = hardware["cuda_error"]
    gpu = hardware["gpu"]
    gpu_ready = hardware["gpu_ready"]
    active_embedding_device = embedding_device()

    if gpu_ready:
        headline = "LOCAL / GPU READY"
        transcription = {"mode": "gpu", "label": "NVIDIA GPU (CUDA)", "detail": "Ready for the next transcription"}
    elif configured_cuda:
        headline = "LOCAL / CPU FALLBACK"
        transcription = {
            "mode": "cpu",
            "label": "CPU fallback",
            "detail": (cuda_error or "NVIDIA driver was not detected") + ". Transcription will continue on CPU.",
        }
    else:
        headline = "LOCAL / CPU MODE"
        transcription = {"mode": "cpu", "label": "CPU", "detail": "CUDA was disabled in configuration"}

    if active_embedding_device == "cuda":
        embeddings = {"mode": "gpu", "label": "NVIDIA GPU (active)"}
    elif active_embedding_device == "cpu":
        embeddings = {"mode": "cpu", "label": "CPU fallback (active)"}
    elif gpu_ready and embedding_installation_supports_cuda():
        embeddings = {"mode": "gpu", "label": "GPU ready; loaded on first search"}
    else:
        embeddings = {"mode": "cpu", "label": "CPU-only build; loaded on first search"}

    return {"headline": headline, "gpu": gpu, "transcription": transcription, "embeddings": embeddings}
