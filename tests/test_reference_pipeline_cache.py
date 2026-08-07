from contextlib import nullcontext

import pytest

from app.services import pipeline


class _Connection:
    def __init__(self, writes):
        self.writes = writes

    def execute(self, sql, parameters=()):
        self.writes.append((sql, parameters))


def _install_reference_fakes(monkeypatch, tmp_path):
    source = tmp_path / "reference.mp4"
    source.write_bytes(b"stable reference clip contents")
    data_dir = tmp_path / "data"
    monkeypatch.setattr(pipeline.settings, "clipfinder_data_dir", data_dir)
    pipeline.settings.work_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pipeline.settings, "whisper_model", "large-v3")

    calls = {
        "extract": [],
        "duration": 0,
        "transcribe": [],
        "embed": 0,
    }
    writes = []
    runtime = {"device": "cpu", "compute_type": "int8"}

    def fake_extract(_source, output, audio_track=1, sample_rate=16000):
        calls["extract"].append((audio_track, sample_rate))
        output.write_bytes(b"temporary wave")

    def fake_duration(_source):
        calls["duration"] += 1
        return 12.0

    def fake_transcribe(
        _audio_path,
        _progress,
        _duration=None,
        _progress_start=18,
        _progress_end=62,
        model_name=None,
        runtime_info=None,
    ):
        calls["transcribe"].append(
            (model_name, runtime["device"], runtime["compute_type"])
        )
        if runtime_info is not None:
            runtime_info.update(
                pipeline.transcription_cache_parameters(
                    model_name,
                    runtime["device"],
                    runtime["compute_type"],
                )
            )
        return [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "Ciekawy klip wzorcowy.",
                "words": [
                    {"start": 0.0, "end": 0.5, "word": "Ciekawy"},
                ],
            }
        ]

    def fake_embed(texts):
        calls["embed"] += 1
        assert texts == ["Ciekawy klip wzorcowy."]
        return [[0.1, 0.2, 0.3]]

    monkeypatch.setattr(pipeline, "extract_audio", fake_extract)
    monkeypatch.setattr(pipeline, "duration_seconds", fake_duration)
    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "embed_texts", fake_embed)
    monkeypatch.setattr(
        pipeline,
        "resolved_transcription_runtime",
        lambda _model: (runtime["device"], runtime["compute_type"], None),
    )
    monkeypatch.setattr(
        pipeline.db,
        "connection",
        lambda: nullcontext(_Connection(writes)),
    )

    return source, calls, writes, runtime


def _import(source, *, source_key=None):
    keys = {source.resolve(): source_key} if source_key else None
    return pipeline.import_reference_files(
        "collection-1", [source], lambda *_args: None, keys,
    )


def test_reimport_reuses_transcription_and_embedding_without_extracting_wav(monkeypatch, tmp_path):
    source, calls, writes, _runtime = _install_reference_fakes(monkeypatch, tmp_path)

    assert _import(source, source_key="https://example.test/short") == 1
    assert _import(source, source_key="https://example.test/short") == 1

    assert calls == {
        "extract": [(1, 16000)],
        "duration": 1,
        "transcribe": [("large-v3", "cpu", "int8")],
        "embed": 1,
    }
    assert len(writes) == 2
    assert writes[-1][1][2] == "https://example.test/short"
    assert not list(pipeline.settings.work_dir.glob("reference-*.wav"))


def test_source_change_invalidates_reference_cache(monkeypatch, tmp_path):
    source, calls, _writes, _runtime = _install_reference_fakes(monkeypatch, tmp_path)

    _import(source)
    _import(source)
    source.write_bytes(b"changed reference clip contents")
    _import(source)

    assert len(calls["transcribe"]) == 2
    assert calls["embed"] == 2
    assert calls["extract"] == [(1, 16000), (1, 16000)]
    assert not list(pipeline.settings.work_dir.glob("reference-*.wav"))


def test_same_reference_text_reuses_embedding_across_transcription_inputs(monkeypatch, tmp_path):
    source, calls, _writes, runtime = _install_reference_fakes(monkeypatch, tmp_path)

    _import(source)

    monkeypatch.setattr(pipeline.settings, "whisper_model", "small")
    _import(source)

    runtime.update(device="cuda", compute_type="float16")
    _import(source)

    monkeypatch.setattr(pipeline, "REFERENCE_AUDIO_TRACK", 2)
    _import(source)

    assert calls["transcribe"] == [
        ("large-v3", "cpu", "int8"),
        ("small", "cpu", "int8"),
        ("small", "cuda", "float16"),
        ("small", "cuda", "float16"),
    ]
    assert calls["embed"] == 1
    assert calls["extract"] == [
        (1, 16000),
        (1, 16000),
        (1, 16000),
        (2, 16000),
    ]


def test_cache_failure_falls_back_to_normal_reference_import(monkeypatch, tmp_path):
    source, calls, _writes, _runtime = _install_reference_fakes(monkeypatch, tmp_path)

    class _UnavailableCache:
        def __init__(self, *_args, **_kwargs):
            raise OSError("cache locked")

    monkeypatch.setattr(pipeline, "PipelineCache", _UnavailableCache)

    assert _import(source) == 1
    assert len(calls["transcribe"]) == 1
    assert calls["embed"] == 1
    assert not list(pipeline.settings.work_dir.glob("reference-*.wav"))


def test_reference_wav_is_removed_when_transcription_fails(monkeypatch, tmp_path):
    source, _calls, writes, _runtime = _install_reference_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        pipeline,
        "transcribe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("decoder failed")),
    )

    with pytest.raises(RuntimeError, match="decoder failed"):
        _import(source)

    assert writes == []
    assert not list(pipeline.settings.work_dir.glob("reference-*.wav"))
