from __future__ import annotations

import asyncio
import io
import json

import pytest
from fastapi import HTTPException, UploadFile

from app import database, main
from app.services import diagnostics


def _segment(segment_id: str) -> dict:
    return {
        "id": segment_id,
        "revision_number": 1,
        "keywords": "[]",
        "tags": "[]",
        "word_timestamps": "[]",
        "short_potential_signals": "[]",
        "quality_signals": "[]",
        "boundary_signals": "[]",
        "context_signals": "[]",
        "extended_story_signals": "[]",
        "chat_messages": "[]",
        "censor_profanity": 0,
        "remove_pauses": 0,
    }


def test_diagnostic_event_redacts_url_and_local_path(monkeypatch):
    captured: list[str] = []

    class CaptureLogger:
        def error(self, template, *values):
            captured.append(template % values)

    monkeypatch.setattr(diagnostics, "logger", lambda: CaptureLogger())
    diagnostics.log_failure(
        r"remote preview url=https://example.test/private path=C:\Users\Tester\secret.mp4",
        RuntimeError("download failed"),
    )

    assert captured
    assert "example.test" not in captured[0]
    assert "secret.mp4" not in captured[0]
    assert "[url]" in captured[0]
    assert "[path]" in captured[0]


def test_diagnostic_report_redacts_forward_slash_and_unc_paths_at_export(tmp_path, monkeypatch):
    log = tmp_path / "clipfinder.log"
    log.write_text(
        "loaded C:/Users/Tester/private/video.mp4\nopened \\\\server\\secret\\clip.mp4\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(diagnostics, "log_path", lambda: log)

    report = diagnostics.build_report({}, "1.0.0")

    assert "Tester" not in report
    assert "server" not in report
    assert "video.mp4" not in report
    assert report.count("[path]") >= 2


def test_analysis_estimate_uses_the_latest_eight_samples(monkeypatch):
    history = [
        {"duration_seconds": 100, "analysis_seconds": ratio * 100}
        for ratio in [1, 2, 3, 4, 5, 6, 7, 8, 100]
    ]
    monkeypatch.setattr(main.db, "rows", lambda *_args, **_kwargs: history)

    estimate, samples = main.estimate_analysis_duration({
        "id": "new-video",
        "duration_seconds": 100,
        "status": "processing",
        "audio_analysis_mode": "single",
        "analysis_mode": "default",
    })

    assert estimate == 450.0
    assert samples == 8


def test_segment_list_batches_current_revision_lookup(monkeypatch):
    segments = [_segment("one"), _segment("two")]
    reviews = {
        item["id"]: {
            "rating": "accepted",
            "review_reason": "",
            "censor_profanity": 0,
            "remove_pauses": 0,
            "archive_audio_path": "",
            "archive_audio_track": 1,
            "reviewed_revision_id": f"revision-{item['id']}",
        }
        for item in segments
    }
    monkeypatch.setattr(database, "_tag_feedback_by_segment", lambda _ids: {})
    monkeypatch.setattr(database, "_reviews_by_segment", lambda _ids: reviews)
    monkeypatch.setattr(
        database,
        "_current_revisions_by_segment",
        lambda ids: {segment_id: f"revision-{segment_id}" for segment_id in ids},
    )
    monkeypatch.setattr(database, "row", lambda *_args, **_kwargs: pytest.fail("per-segment revision query"))

    result = database.serialize_segments(segments)

    assert [item["review_stale"] for item in result] == [False, False]


def test_chat_upload_reader_rejects_data_immediately_above_limit():
    upload = UploadFile(filename="chat.jsonl", file=io.BytesIO(b"123456"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(main._read_upload_limited(upload, limit=5))
    assert error.value.status_code == 400
    asyncio.run(upload.close())


def test_chat_upload_closes_file_and_runs_import(monkeypatch):
    upload = UploadFile(filename="chat.jsonl", file=io.BytesIO(json.dumps({"message": "hello"}).encode()))
    monkeypatch.setattr(main.db, "row", lambda *_args, **_kwargs: {"id": "video"})
    monkeypatch.setattr(main, "import_chat", lambda *args: {"available": True, "source_name": args[1]})

    result = asyncio.run(main.upload_video_chat("video", upload, 6))

    assert result["available"] is True
    assert upload.file.closed


def test_audio_preview_check_validates_track_without_transcoding(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    segment = {
        "id": "clip", "path": str(source), "source_removed": 0,
        "start_seconds": 10.0, "end_seconds": 20.0, "remove_pauses": 0,
    }
    monkeypatch.setattr(main.db, "row", lambda *_args, **_kwargs: segment)
    monkeypatch.setattr(main, "audio_track_count", lambda _source: 2)
    monkeypatch.setattr(main, "export_audio_preview", lambda *_args, **_kwargs: pytest.fail("check must not transcode"))

    result = main.check_audio_preview("clip", audio_track=2, remove_pauses=False)

    assert result == {
        "status": "ok", "audio_track": 2, "remove_pauses": False,
        "archived": False, "duration_seconds": 10.0,
    }


def test_archived_audio_rejects_changed_pause_setting(monkeypatch, tmp_path):
    archive = tmp_path / "clip.mp3"
    archive.write_bytes(b"audio")
    segment = {
        "id": "clip", "path": str(tmp_path / "removed.mp4"), "source_removed": 1,
        "archive_audio_path": str(archive), "archive_audio_track": 1,
        "start_seconds": 0.0, "end_seconds": 5.0, "remove_pauses": 1,
    }
    monkeypatch.setattr(main.db, "row", lambda *_args, **_kwargs: segment)

    with pytest.raises(HTTPException) as error:
        main.check_audio_preview("clip", audio_track=1, remove_pauses=False)

    assert error.value.status_code == 409


def test_edited_utterance_refreshes_context_scores_of_nearby_segments(monkeypatch):
    monkeypatch.setattr(
        main.db,
        "rows",
        lambda *_args, **_kwargs: [
            {"id": "before", "start_seconds": 0.0, "end_seconds": 9.0},
            {"id": "after", "start_seconds": 21.0, "end_seconds": 30.0},
        ],
    )
    monkeypatch.setattr(
        main,
        "_segment_context",
        lambda _video, segment_id, _start, _end, _window=16.0: (
            f"before-{segment_id}", f"after-{segment_id}",
        ),
    )
    refreshed: list[tuple[str, set[str], dict]] = []
    monkeypatch.setattr(
        main,
        "_recompute_persisted_segment",
        lambda segment_id, fields, values: refreshed.append((segment_id, fields, values)),
    )

    main._refresh_neighbour_contexts("video", "edited", 10.0, 20.0)

    assert refreshed == [
        (
            "before",
            {"context_before", "context_after"},
            {"context_before": "before-before", "context_after": "after-before"},
        ),
        (
            "after",
            {"context_before", "context_after"},
            {"context_before": "before-after", "context_after": "after-after"},
        ),
    ]


def test_remote_preview_pruning_removes_expired_results(monkeypatch):
    monkeypatch.setattr(main.time, "monotonic", lambda: 5000.0)
    with main._remote_preview_lock:
        main._remote_preview_jobs.clear()
        main._remote_preview_fingerprints.clear()
        main._remote_preview_jobs["expired"] = {
            "id": "expired", "state": "completed", "updated_monotonic": 100.0,
        }
        main._remote_preview_fingerprints["expired"] = {"embedding": [1.0]}

    main._prune_remote_previews()

    assert "expired" not in main._remote_preview_jobs
    assert "expired" not in main._remote_preview_fingerprints
