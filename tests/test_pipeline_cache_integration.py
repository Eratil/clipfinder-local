from contextlib import nullcontext
from dataclasses import dataclass, field
from types import SimpleNamespace

import numpy as np

from app.services import pipeline


class _Connection:
    def execute(self, *_args, **_kwargs):
        return None


@dataclass
class _ExpensiveCalls:
    transcribe: int = 0
    scenes: int = 0
    audio_window: int = 0
    audio_energy: int = 0
    embeddings: int = 0
    visual_interest: int = 0
    visual_reading: int = 0
    transcription_models: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, int]:
        return {
            "transcribe": self.transcribe,
            "scenes": self.scenes,
            "audio_window": self.audio_window,
            "audio_energy": self.audio_energy,
            "embeddings": self.embeddings,
            "visual_interest": self.visual_interest,
            "visual_reading": self.visual_reading,
        }


def _install_pipeline_fakes(monkeypatch, tmp_path):
    """Run the real cache integration while replacing all expensive stages."""
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"stable recording contents")
    data_dir = tmp_path / "data"
    export_defaults = {
        "game_x": 0.12,
        "game_y": 0.08,
        "game_width": 0.76,
        "game_height": 0.84,
    }
    video = {
        "id": "video-cache-integration",
        "path": str(source),
        "source_url": "",
        "analysis_mode": "default",
    }
    audio_defaults = {
        "mode": "split",
        "microphone_track": 2,
        "use_all_sounds": False,
        "all_sounds_track": 1,
        "use_game": True,
        "game_track": 3,
    }
    candidate = {
        "start": 4.0,
        "end": 18.0,
        "text": "To jest kompletna wypowiedz i ciekawa reakcja.",
        "words": [{"start": 4.0, "end": 4.3, "word": "To"}],
        "context_before": "Kontekst przed.",
        "context_after": "Kontekst po.",
        "boundary_signals": ["start aligned to sentence"],
    }
    transcript = [
        {
            "start": 4.0,
            "end": 18.0,
            "text": candidate["text"],
            "words": candidate["words"],
        }
    ]
    calls = _ExpensiveCalls()

    def fake_row(sql, _parameters=()):
        if "FROM videos" in sql:
            return video
        if "analysis_audio_defaults" in sql:
            return audio_defaults
        if "export_defaults" in sql:
            return export_defaults
        raise AssertionError(f"Unexpected query: {sql}")

    def fake_transcribe(
        _audio_path,
        _progress,
        _duration=None,
        _progress_start=18,
        _progress_end=62,
        model_name=None,
        runtime_info=None,
    ):
        selected_model = model_name or pipeline.settings.whisper_model
        calls.transcribe += 1
        calls.transcription_models.append(selected_model)
        if runtime_info is not None:
            runtime_info.update(
                pipeline.transcription_cache_parameters(selected_model, "cpu", "int8")
            )
        return transcript

    def fake_scenes(_source):
        calls.scenes += 1
        return [12.0]

    def fake_audio_window(_audio_path):
        calls.audio_window += 1
        return (
            np.asarray([0.1, 0.4, 0.2], dtype=np.float32),
            np.asarray([0.2, 0.3, 0.4], dtype=np.float32),
        )

    def fake_audio_energy(_audio_path):
        calls.audio_energy += 1
        return np.asarray([0.1, 0.8, 0.2], dtype=np.float32)

    def fake_embeddings(texts):
        calls.embeddings += 1
        return [[0.1, 0.2] for _text in texts]

    def fake_visual_interest(_source, records, **_kwargs):
        calls.visual_interest += 1
        return {record["id"]: 8 for record in records}

    def fake_visual_reading(_source, records, **_kwargs):
        calls.visual_reading += 1
        return {record["id"]: 4 for record in records}

    monkeypatch.setattr(pipeline.db, "row", fake_row)
    monkeypatch.setattr(pipeline.db, "connection", lambda: nullcontext(_Connection()))
    monkeypatch.setattr(pipeline.settings, "clipfinder_data_dir", data_dir)
    monkeypatch.setattr(pipeline.settings, "whisper_model", "large-v3")
    monkeypatch.setattr(
        pipeline,
        "resolved_transcription_runtime",
        lambda _model: ("cpu", "int8", None),
    )
    monkeypatch.setattr(pipeline, "start_analysis_run", lambda *_args: "run-cache")
    monkeypatch.setattr(pipeline, "update_analysis_run_inputs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "duration_seconds", lambda _source: 30.0)
    monkeypatch.setattr(pipeline, "audio_track_count", lambda _source: 3)
    monkeypatch.setattr(pipeline, "extract_audio", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "detect_boundaries", fake_scenes)
    monkeypatch.setattr(
        pipeline,
        "build_candidates",
        lambda *_args, **_kwargs: [dict(candidate)],
    )
    monkeypatch.setattr(pipeline, "audio_window_features", fake_audio_window)
    monkeypatch.setattr(pipeline, "audio_energy_windows", fake_audio_energy)
    monkeypatch.setattr(
        pipeline,
        "voice_delivery_scores",
        lambda _energies, _tones, candidates: [7] * len(candidates),
    )
    monkeypatch.setattr(
        pipeline,
        "game_reaction_scores",
        lambda _game, _microphone, candidates, **_kwargs: [6] * len(candidates),
    )
    monkeypatch.setattr(pipeline, "embed_texts", fake_embeddings)
    monkeypatch.setattr(pipeline, "infer_tags", lambda _text, _vector: ["humor"])
    monkeypatch.setattr(pipeline, "visual_interest_scores", fake_visual_interest)
    monkeypatch.setattr(pipeline, "visual_reading_scores", fake_visual_reading)
    monkeypatch.setattr(
        pipeline,
        "recompute_segment_features",
        lambda _record: SimpleNamespace(updates={}),
    )
    monkeypatch.setattr(pipeline, "assign_duplicate_groups", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "persist_analysis_results",
        lambda *_args, **_kwargs: {"matched": 0, "retired": 0},
    )
    monkeypatch.setattr(pipeline, "apply_chat_reactions", lambda _video_id: None)

    return calls, export_defaults, video, audio_defaults, source


def _analyse_once():
    pipeline.analyse("video-cache-integration", lambda *_args: None)


def test_warm_analysis_reuses_every_expensive_cached_stage(monkeypatch, tmp_path):
    calls, _export_defaults, _video, _audio_defaults, _source = _install_pipeline_fakes(monkeypatch, tmp_path)

    _analyse_once()
    cold_counts = calls.snapshot()
    assert cold_counts == {
        "transcribe": 1,
        "scenes": 0,
        "audio_window": 1,
        "audio_energy": 1,
        "embeddings": 1,
        "visual_interest": 1,
        "visual_reading": 1,
    }

    _analyse_once()

    assert calls.snapshot() == cold_counts


def test_cpu_fallback_transcript_cache_survives_a_new_cuda_probe(monkeypatch, tmp_path):
    calls, _export_defaults, _video, _audio_defaults, _source = _install_pipeline_fakes(
        monkeypatch, tmp_path,
    )
    # The preflight says CUDA is available on both launches, while the mocked
    # transcription reports that model initialization actually used CPU.
    monkeypatch.setattr(
        pipeline,
        "resolved_transcription_runtime",
        lambda _model: ("cuda", "float16", None),
    )

    _analyse_once()
    _analyse_once()

    assert calls.transcribe == 1


def test_gameplay_rect_invalidates_only_visual_interest(monkeypatch, tmp_path):
    calls, export_defaults, _video, _audio_defaults, _source = _install_pipeline_fakes(monkeypatch, tmp_path)

    _analyse_once()
    cold_counts = calls.snapshot()
    export_defaults.update(
        {"game_x": 0.03, "game_y": 0.15, "game_width": 0.91, "game_height": 0.72}
    )

    _analyse_once()

    assert calls.visual_interest == cold_counts["visual_interest"] + 1
    assert calls.snapshot() | {"visual_interest": cold_counts["visual_interest"]} == cold_counts


def test_transcription_model_change_is_a_cache_miss(monkeypatch, tmp_path):
    calls, _export_defaults, _video, _audio_defaults, _source = _install_pipeline_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline.settings, "whisper_model", "small")

    _analyse_once()
    _analyse_once()
    assert calls.transcribe == 1
    assert calls.transcription_models == ["small"]

    monkeypatch.setattr(pipeline.settings, "whisper_model", "large-v3")
    _analyse_once()

    assert calls.transcribe == 2
    assert calls.transcription_models == ["small", "large-v3"]


def test_default_to_extended_reuses_raw_stage_results(monkeypatch, tmp_path):
    calls, _export_defaults, video, _audio_defaults, _source = _install_pipeline_fakes(
        monkeypatch, tmp_path,
    )

    _analyse_once()
    cold_counts = calls.snapshot()
    video["analysis_mode"] = "extended"

    _analyse_once()

    assert calls.scenes == cold_counts["scenes"] + 1
    assert calls.snapshot() | {"scenes": cold_counts["scenes"]} == cold_counts

    _analyse_once()
    assert calls.scenes == 1


def test_microphone_track_change_invalidates_only_track_dependent_stages(monkeypatch, tmp_path):
    calls, _export_defaults, _video, audio_defaults, _source = _install_pipeline_fakes(
        monkeypatch, tmp_path,
    )

    _analyse_once()
    cold_counts = calls.snapshot()
    audio_defaults["microphone_track"] = 1

    _analyse_once()

    assert calls.transcribe == cold_counts["transcribe"] + 1
    assert calls.audio_window == cold_counts["audio_window"] + 1
    assert calls.audio_energy == cold_counts["audio_energy"]
    assert calls.scenes == cold_counts["scenes"]
    assert calls.embeddings == cold_counts["embeddings"]
    assert calls.visual_interest == cold_counts["visual_interest"]
    assert calls.visual_reading == cold_counts["visual_reading"]


def test_changed_source_at_same_path_invalidates_every_raw_stage(monkeypatch, tmp_path):
    calls, _export_defaults, _video, _audio_defaults, source = _install_pipeline_fakes(
        monkeypatch, tmp_path,
    )

    _analyse_once()
    cold_counts = calls.snapshot()
    source.write_bytes(b"replacement recording contents with a different identity")

    _analyse_once()

    assert calls.scenes == cold_counts["scenes"]
    assert all(
        calls.snapshot()[name] == count + 1
        for name, count in cold_counts.items()
        if name != "scenes"
    )
