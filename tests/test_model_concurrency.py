from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


def test_embedding_model_load_and_encode_are_serialized(monkeypatch):
    from app.services import embeddings

    first_encode_started = threading.Event()
    second_encode_started = threading.Event()
    state_lock = threading.Lock()
    state = {"constructors": 0, "encode_calls": 0, "active": 0, "max_active": 0}

    class FakeSentenceTransformer:
        def __init__(self, _name: str, device: str, revision: str):
            assert device == "cuda"
            assert revision == embeddings.EMBEDDING_MODEL_REVISION
            with state_lock:
                state["constructors"] += 1

        def encode(self, texts, **_kwargs):
            with state_lock:
                state["encode_calls"] += 1
                call_number = state["encode_calls"]
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            if call_number == 1:
                first_encode_started.set()
                second_encode_started.wait(timeout=0.2)
            else:
                second_encode_started.set()
            try:
                return np.asarray([[float(len(text)), 1.0] for text in texts])
            finally:
                with state_lock:
                    state["active"] -= 1

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=FakeSentenceTransformer),
    )
    monkeypatch.setattr(embeddings, "installation_supports_cuda", lambda: True)
    embeddings._model = None
    embeddings._model_device = None
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(embeddings.embed_texts, ["pierwszy"])
            assert first_encode_started.wait(timeout=1)
            second = executor.submit(embeddings.embed_texts, ["drugi"])
            assert first.result(timeout=2) == [[8.0, 1.0]]
            assert second.result(timeout=2) == [[5.0, 1.0]]

        assert state["constructors"] == 1
        assert state["max_active"] == 1
        assert embeddings.current_device() == "cuda"
    finally:
        embeddings._model = None
        embeddings._model_device = None


def test_whisper_model_load_and_transcribe_are_serialized(monkeypatch):
    from app.services import pipeline

    first_transcribe_started = threading.Event()
    second_transcribe_started = threading.Event()
    state_lock = threading.Lock()
    state = {"constructors": 0, "calls": 0, "active": 0, "max_active": 0}

    class FakePart:
        start = 0.0
        end = 1.0
        text = " test "
        words = []

    class FakeWhisperModel:
        def __init__(self, _name: str, *, device: str, compute_type: str):
            assert (device, compute_type) == ("cpu", "int8")
            with state_lock:
                state["constructors"] += 1

        def transcribe(self, _path: str, **_kwargs):
            def lazy_parts():
                with state_lock:
                    state["calls"] += 1
                    call_number = state["calls"]
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                if call_number == 1:
                    first_transcribe_started.set()
                    second_transcribe_started.wait(timeout=0.2)
                else:
                    second_transcribe_started.set()
                try:
                    yield FakePart()
                finally:
                    with state_lock:
                        state["active"] -= 1

            return lazy_parts(), object()

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    monkeypatch.setattr(
        pipeline,
        "resolved_transcription_runtime",
        lambda _model_name: ("cpu", "int8", None),
    )
    monkeypatch.setattr(pipeline, "whisper_model_source", lambda model_name: model_name)
    pipeline._transcription_models.clear()
    pipeline._failed_transcription_runtimes.clear()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(pipeline.transcribe, Path("first.wav"), lambda *_args: None)
            assert first_transcribe_started.wait(timeout=1)
            second = executor.submit(pipeline.transcribe, Path("second.wav"), lambda *_args: None)
            assert first.result(timeout=2)[0]["text"] == "test"
            assert second.result(timeout=2)[0]["text"] == "test"

        assert state["constructors"] == 1
        assert state["max_active"] == 1
    finally:
        pipeline._transcription_models.clear()
        pipeline._failed_transcription_runtimes.clear()


def test_lazy_gpu_transcription_failure_retries_on_cpu(monkeypatch):
    from app.services import pipeline

    constructed: list[tuple[str, str]] = []

    class FakePart:
        start = 0.0
        end = 1.0
        text = " cpu result "
        words = []

    class FakeWhisperModel:
        def __init__(self, _name: str, *, device: str, compute_type: str):
            self.device = device
            constructed.append((device, compute_type))

        def transcribe(self, _path: str, **_kwargs):
            if self.device == "cuda":
                def broken_generator():
                    raise RuntimeError("lazy CUDA failure")
                    yield  # pragma: no cover

                return broken_generator(), object()
            return iter([FakePart()]), object()

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisperModel))
    monkeypatch.setattr(pipeline, "resolved_transcription_runtime", lambda _name: ("cuda", "float16", None))
    monkeypatch.setattr(pipeline, "whisper_model_source", lambda name: name)
    monkeypatch.setattr(pipeline.diagnostics, "log_failure", lambda *_args: None)
    pipeline._transcription_models.clear()
    pipeline._failed_transcription_runtimes.clear()
    runtime: dict = {}
    try:
        result = pipeline.transcribe(Path("clip.wav"), lambda *_args: None, runtime_info=runtime)

        assert result[0]["text"] == "cpu result"
        assert constructed == [("cuda", "float16"), ("cpu", "int8")]
        assert runtime["device"] == "cpu"
        assert any(key[2:] == ("cuda", "float16") for key in pipeline._failed_transcription_runtimes)
    finally:
        pipeline._transcription_models.clear()
        pipeline._failed_transcription_runtimes.clear()


def test_model_download_failure_does_not_mark_cuda_runtime_failed(monkeypatch):
    from app.services import pipeline

    monkeypatch.setattr(pipeline, "resolved_transcription_runtime", lambda _name: ("cuda", "float16", None))
    monkeypatch.setattr(
        pipeline,
        "whisper_model_source",
        lambda _name: (_ for _ in ()).throw(OSError("offline")),
    )
    pipeline._transcription_models.clear()
    pipeline._failed_transcription_runtimes.clear()
    try:
        with pytest.raises(OSError, match="offline"):
            pipeline.transcribe(Path("clip.wav"), lambda *_args: None)
        assert not pipeline._failed_transcription_runtimes
    finally:
        pipeline._transcription_models.clear()
        pipeline._failed_transcription_runtimes.clear()
