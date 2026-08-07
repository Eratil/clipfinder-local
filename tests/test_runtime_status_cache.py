from __future__ import annotations

import importlib


def test_runtime_status_caches_slow_hardware_probe_but_not_model_state(monkeypatch):
    runtime_module = importlib.import_module("app.services.runtime_status")
    calls = {"hardware": 0, "embedding": 0}

    def hardware():
        calls["hardware"] += 1
        return {
            "configured_cuda": True,
            "cuda_error": None,
            "gpu": {"name": "Test GPU", "memory_mb": "8192", "driver": "1.0"},
            "gpu_ready": True,
        }

    def embedding():
        calls["embedding"] += 1
        return None if calls["embedding"] == 1 else "cuda"

    monkeypatch.setattr(runtime_module, "_probe_hardware", hardware)
    monkeypatch.setattr(runtime_module, "embedding_device", embedding)
    monkeypatch.setattr(runtime_module, "embedding_installation_supports_cuda", lambda: True)
    runtime_module.invalidate_runtime_status_cache()

    first = runtime_module.runtime_status()
    second = runtime_module.runtime_status()

    assert calls == {"hardware": 1, "embedding": 2}
    assert first["embeddings"]["label"] == "GPU ready; loaded on first search"
    assert second["embeddings"]["label"] == "NVIDIA GPU (active)"

    runtime_module.invalidate_runtime_status_cache()
    runtime_module.runtime_status()
    assert calls == {"hardware": 2, "embedding": 3}
    runtime_module.invalidate_runtime_status_cache()


def test_cpu_only_embedding_wheel_is_reported_before_model_load(monkeypatch):
    runtime_module = importlib.import_module("app.services.runtime_status")
    monkeypatch.setattr(
        runtime_module,
        "_probe_hardware",
        lambda: {"configured_cuda": True, "cuda_error": None, "gpu": {"name": "GPU"}, "gpu_ready": True},
    )
    monkeypatch.setattr(runtime_module, "embedding_device", lambda: None)
    monkeypatch.setattr(runtime_module, "embedding_installation_supports_cuda", lambda: False)
    runtime_module.invalidate_runtime_status_cache()

    status = runtime_module.runtime_status()

    assert status["embeddings"] == {"mode": "cpu", "label": "CPU-only build; loaded on first search"}
    runtime_module.invalidate_runtime_status_cache()


def test_transcription_readiness_does_not_require_nvidia_smi_on_path(monkeypatch):
    runtime_module = importlib.import_module("app.services.runtime_status")
    monkeypatch.setattr(runtime_module.settings, "whisper_device", "cuda")
    monkeypatch.setattr(runtime_module, "cuda12_runtime_error", lambda: None)
    monkeypatch.setattr(runtime_module, "_nvidia_gpu", lambda: None)

    hardware = runtime_module._probe_hardware()

    assert hardware["gpu"] is None
    assert hardware["gpu_ready"] is True
