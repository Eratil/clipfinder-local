"""Small, non-invasive runtime diagnostics for the desktop interface."""
from __future__ import annotations

import os
import subprocess

from app.config import settings
from app.services.cuda_runtime import cuda12_runtime_error
from app.services.embeddings import current_device as embedding_device


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


def runtime_status() -> dict:
    """Return what the app can really use now, without loading any ML model."""
    configured_cuda = settings.whisper_device.lower() == "cuda"
    cuda_error = cuda12_runtime_error() if configured_cuda else None
    gpu = _nvidia_gpu()
    gpu_ready = configured_cuda and not cuda_error and gpu is not None
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
    elif gpu_ready:
        embeddings = {"mode": "gpu", "label": "GPU ready; loaded on first search"}
    else:
        embeddings = {"mode": "cpu", "label": "CPU; loaded on first search"}

    return {"headline": headline, "gpu": gpu, "transcription": transcription, "embeddings": embeddings}
