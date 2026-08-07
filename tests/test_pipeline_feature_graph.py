from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest

from app.services import pipeline


class _Connection:
    def execute(self, *_args, **_kwargs):
        return None


def test_analysis_recomputes_features_once_after_visual_evidence(monkeypatch, tmp_path):
    """The pipeline supplies raw evidence and lets the graph own derivation."""
    video = {
        "id": "video-1",
        "path": str(tmp_path / "recording.mp4"),
        "source_url": "",
        "analysis_mode": "default",
    }
    audio_defaults = {"mode": "single", "single_track": 1}

    def fake_row(sql, _parameters=()):
        if "FROM videos" in sql:
            return video
        if "analysis_audio_defaults" in sql:
            return audio_defaults
        if "export_defaults" in sql:
            return {}
        raise AssertionError(f"Unexpected query: {sql}")

    candidate = {
        "start": 4.0,
        "end": 18.0,
        "text": "To jest kompletna wypowiedź z reakcją.",
        "words": [{"start": 4.0, "end": 4.4, "word": "To"}],
        "context_before": "Wcześniejszy kontekst.",
        "context_after": "Późniejszy kontekst.",
        "boundary_signals": ["start aligned to sentence"],
    }
    graph_inputs = []
    persisted = []

    monkeypatch.setattr(pipeline.db, "row", fake_row)
    monkeypatch.setattr(pipeline.db, "connection", lambda: nullcontext(_Connection()))
    monkeypatch.setattr(pipeline.settings, "clipfinder_data_dir", tmp_path)
    monkeypatch.setattr(pipeline, "start_analysis_run", lambda *_args: "run-1")
    monkeypatch.setattr(pipeline, "duration_seconds", lambda _path: 30.0)
    monkeypatch.setattr(pipeline, "audio_track_count", lambda _path: 1)
    monkeypatch.setattr(pipeline, "extract_audio", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "transcribe", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(pipeline, "detect_boundaries", lambda _path: [])
    monkeypatch.setattr(pipeline, "build_candidates", lambda *_args, **_kwargs: [candidate])
    monkeypatch.setattr(pipeline, "audio_energy_windows", lambda _path: np.asarray([], dtype=np.float32))
    monkeypatch.setattr(pipeline, "embed_texts", lambda _texts: [[0.1, 0.2]])
    monkeypatch.setattr(pipeline, "infer_tags", lambda _text, _vector: ["humor"])
    monkeypatch.setattr(pipeline, "visual_interest_scores", lambda *_args, **_kwargs: {"candidate": 12})
    monkeypatch.setattr(
        pipeline,
        "visual_reading_scores",
        lambda _path, records: {records[0]["id"]: 9},
    )

    def fake_recompute(record):
        graph_inputs.append(dict(record))
        return SimpleNamespace(updates={
            "tags": ["humor", "reading"],
            "quality_score": 41,
            "quality_signals": ["central graph"],
            "short_potential_score": 38,
            "short_potential_signals": ["central graph"],
            "visual_reading_likelihood": 0.835,
            "reading_likelihood": 0.83,
            "logical_sense_score": 35,
            "context_score": 35,
            "context_signals": ["reading context"],
            "self_contained_score": 35,
            "extended_completeness_score": -1,
            "moment_reaction_score": 0,
            "moment_reaction_stage": "none",
        })

    monkeypatch.setattr(pipeline, "recompute_segment_features", fake_recompute)
    monkeypatch.setattr(pipeline, "assign_duplicate_groups", lambda records, **_kwargs: None)

    def fake_persist(_video_id, _run_id, records):
        persisted.extend(records)
        return {"matched": 0, "retired": 0}

    monkeypatch.setattr(pipeline, "persist_analysis_results", fake_persist)
    monkeypatch.setattr(pipeline, "apply_chat_reactions", lambda _video_id: None)

    pipeline.analyse("video-1", lambda *_args: None)

    assert len(graph_inputs) == 1
    raw = graph_inputs[0]
    assert raw["analysis_mode"] == "default"
    assert raw["boundary_signals"] == ["start aligned to sentence"]
    assert raw["context_before"] == "Wcześniejszy kontekst."
    assert raw["context_after"] == "Późniejszy kontekst."
    assert raw["reading_screen_score"] == 9
    assert raw["vision_score"] == 0
    assert "quality_score" not in raw
    assert "logical_sense_score" not in raw
    assert "short_potential_score" not in raw
    assert persisted[0]["quality_score"] == 41
    assert persisted[0]["short_potential_score"] == 38
    assert persisted[0]["visual_reading_likelihood"] == 0.835


def test_analysis_removes_intermediate_audio_after_failure(monkeypatch, tmp_path):
    video = {
        "id": "video-failure",
        "path": str(tmp_path / "recording.mp4"),
        "source_url": "",
        "analysis_mode": "default",
    }

    def fake_row(sql, _parameters=()):
        if "FROM videos" in sql:
            return video
        if "analysis_audio_defaults" in sql:
            return {"mode": "single", "single_track": 1}
        if "export_defaults" in sql:
            return {}
        raise AssertionError(f"Unexpected query: {sql}")

    monkeypatch.setattr(pipeline.db, "row", fake_row)
    monkeypatch.setattr(pipeline.db, "connection", lambda: nullcontext(_Connection()))
    monkeypatch.setattr(pipeline.settings, "clipfinder_data_dir", tmp_path)
    pipeline.settings.work_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pipeline, "start_analysis_run", lambda *_args: "run-failure")
    monkeypatch.setattr(pipeline, "duration_seconds", lambda _path: 60.0)
    monkeypatch.setattr(pipeline, "audio_track_count", lambda _path: 1)

    def fake_extract(_source, output, *_args, **_kwargs):
        output.write_bytes(b"large temporary wave")

    monkeypatch.setattr(pipeline, "extract_audio", fake_extract)
    monkeypatch.setattr(
        pipeline,
        "transcribe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("decoder failed")),
    )

    expected_audio = pipeline.settings.work_dir / "video-failure.wav"
    with pytest.raises(RuntimeError, match="decoder failed"):
        pipeline.analyse("video-failure", lambda *_args: None)
    assert not expected_audio.exists()
