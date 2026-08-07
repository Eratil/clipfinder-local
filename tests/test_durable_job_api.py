from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path

import pytest
from fastapi import UploadFile

from app import database as db
from app import main
from app.config import settings
from app.models import ReferenceFolderImport, ReferenceUrlImport, RemoteVideoCreate
from app.services.analysis_store import persist_analysis_results, start_analysis_run
from app.services.feedback import set_review, set_tag_verdict


@pytest.fixture
def api_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "clipfinder_data_dir", tmp_path)
    monkeypatch.setattr(main, "_durable_worker", None)
    db.initialize()
    return tmp_path


def test_upload_and_reanalysis_share_one_durable_job(api_data_dir: Path) -> None:
    upload = UploadFile(filename="recording.mp4", file=io.BytesIO(b"fake-video"))
    created = asyncio.run(main.upload_video(upload, "default"))

    job = db.row("SELECT * FROM jobs WHERE id=?", (created["job_id"],))
    assert job and job["state"] == "queued" and job["kind"] == "analysis"
    assert Path(db.row("SELECT path FROM videos WHERE id=?", (created["video_id"],))["path"]).read_bytes() == b"fake-video"

    first_retry = main.restart_analysis(created["video_id"])
    second_retry = main.restart_analysis(created["video_id"])
    assert first_retry["job_id"] == created["job_id"]
    assert second_retry["job_id"] == created["job_id"]
    assert db.row("SELECT COUNT(*) AS value FROM jobs WHERE video_id=?", (created["video_id"],))["value"] == 1


def test_health_starts_durable_worker_only_after_api_is_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWorker:
        running = False
        starts = 0

        def start(self) -> None:
            self.starts += 1
            self.running = True

    worker = FakeWorker()
    monkeypatch.setattr(main, "_durable_worker", worker)

    assert main.health()["status"] == "ok"
    assert worker.starts == 1
    assert main.health()["status"] == "ok"
    assert worker.starts == 1


def test_remote_and_reference_endpoints_only_enqueue(api_data_dir: Path) -> None:
    remote = main.import_remote_video(
        RemoteVideoCreate(source_url="https://www.youtube.com/watch?v=example", analysis_mode="fast")
    )
    remote_job = db.row("SELECT * FROM jobs WHERE id=?", (remote["job_id"],))
    assert remote_job and remote_job["kind"] == "remote_import" and remote_job["state"] == "queued"

    collection = main.create_collection(main.CollectionCreate(name="references"))
    folder = api_data_dir / "clips"
    folder.mkdir()
    folder_import = main.import_references(
        collection["id"], ReferenceFolderImport(folder_path=str(folder), include_subfolders=True),
    )
    duplicate = main.import_references(
        collection["id"], ReferenceFolderImport(folder_path=str(folder), include_subfolders=True),
    )
    url_import = main.import_reference_url(
        collection["id"], ReferenceUrlImport(source_url="https://www.youtube.com/shorts/example"),
    )

    assert duplicate["import_id"] == folder_import["import_id"]
    assert db.row("SELECT kind FROM reference_imports WHERE id=?", (folder_import["import_id"],))["kind"] == "folder"
    assert db.row("SELECT kind FROM reference_imports WHERE id=?", (url_import["import_id"],))["kind"] == "url"


def test_lifespan_worker_finishes_a_persisted_upload(
    api_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_analyse(video_id: str, report, _audio_snapshot=None) -> None:
        report(40, "Fake analysis")
        with db.connection() as con:
            con.execute(
                "UPDATE videos SET status='ready', updated_at=? WHERE id=?",
                (db.now(), video_id),
            )
        report(100, "Ready")

    monkeypatch.setattr(main, "analyse", fake_analyse)
    created = asyncio.run(main.upload_video(
        UploadFile(filename="recording.mp4", file=io.BytesIO(b"fake-video")), "fast",
    ))
    # Simulate a hard process exit after the old worker claimed the row.
    with db.connection() as con:
        abandoned = main.job_queue.claim_next(con, worker_id="dead-worker", lease_seconds=3600)
        con.execute("UPDATE videos SET status='processing' WHERE id=?", (created["video_id"],))
    assert abandoned and abandoned["state"] == "running"
    worker = main.build_durable_worker()
    monkeypatch.setattr(main, "_durable_worker", worker)
    worker.start()
    worker.wake()
    deadline = time.monotonic() + 3
    try:
        while time.monotonic() < deadline:
            job = db.row("SELECT state FROM jobs WHERE id=?", (created["job_id"],))
            if job and job["state"] == "completed":
                break
            time.sleep(0.02)
        else:
            pytest.fail("The durable worker did not finish the queued upload")
    finally:
        assert worker.stop(1)

    assert db.row("SELECT status FROM videos WHERE id=?", (created["video_id"],))["status"] == "ready"
    assert db.row("SELECT attempt_count FROM jobs WHERE id=?", (created["job_id"],))["attempt_count"] == 1


def test_recovery_does_not_repeat_analysis_persisted_before_process_exit(api_data_dir: Path) -> None:
    created = asyncio.run(main.upload_video(
        UploadFile(filename="recording.mp4", file=io.BytesIO(b"fake-video")), "default",
    ))
    with db.connection() as con:
        claimed = main.job_queue.claim_next(con, worker_id="dead-worker", lease_seconds=3600)
        timestamp = db.now()
        con.execute(
            "UPDATE videos SET status='ready', updated_at=? WHERE id=?",
            (timestamp, created["video_id"]),
        )
        con.execute(
            """INSERT INTO analysis_runs
               (id, video_id, sequence, state, is_current, started_at, completed_at, created_at)
               VALUES ('persisted-run', ?, 1, 'completed', 1, ?, ?, ?)""",
            (created["video_id"], timestamp, timestamp, timestamp),
        )
    assert claimed and claimed["state"] == "running"

    main._recover_video_jobs()

    job = db.row("SELECT state, progress FROM jobs WHERE id=?", (created["job_id"],))
    assert job == {"state": "completed", "progress": 100}
    assert db.row("SELECT status FROM videos WHERE id=?", (created["video_id"],))["status"] == "ready"


def test_recovery_reconciles_terminal_job_to_recording_card(api_data_dir: Path) -> None:
    created = asyncio.run(main.upload_video(
        UploadFile(filename="recording.mp4", file=io.BytesIO(b"fake-video")), "default",
    ))
    with db.connection() as con:
        claimed = main.job_queue.claim_next(con, worker_id="dead-worker", lease_seconds=3600)
        assert claimed
        failed = main.job_queue.fail(
            con, created["job_id"], claimed["lease_token"], "broken codec", retryable=False,
        )
        assert failed and failed["state"] == "failed"
        con.execute("UPDATE videos SET status='processing', error_message=NULL WHERE id=?", (created["video_id"],))

    main._recover_video_jobs()

    video = db.row("SELECT status, error_message FROM videos WHERE id=?", (created["video_id"],))
    assert video == {"status": "failed", "error_message": "broken codec"}


def test_failed_reanalysis_keeps_previous_successful_result_visible(api_data_dir: Path) -> None:
    created = asyncio.run(main.upload_video(
        UploadFile(filename="recording.mp4", file=io.BytesIO(b"fake-video")), "default",
    ))
    timestamp = db.now()
    with db.connection() as con:
        con.execute(
            """INSERT INTO analysis_runs
               (id, video_id, sequence, state, is_current, started_at, completed_at, created_at)
               VALUES ('previous-run', ?, 1, 'completed', 1, ?, ?, ?)""",
            (created["video_id"], timestamp, timestamp, timestamp),
        )
        claimed = main.job_queue.claim_next(con, worker_id="worker", lease_seconds=60)
        assert claimed
        failed = main.job_queue.fail(
            con, created["job_id"], claimed["lease_token"], "new analysis failed", retryable=False,
        )
    main._sync_video_job(failed)

    video = db.row("SELECT status, error_message FROM videos WHERE id=?", (created["video_id"],))
    assert video == {"status": "ready", "error_message": "new analysis failed"}


def test_analysis_uses_audio_settings_captured_when_job_was_enqueued(
    api_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with db.connection() as con:
        con.execute(
            """UPDATE analysis_audio_defaults
               SET mode='split', microphone_track=2, game_track=3,
                   use_all_sounds=0, use_game=1 WHERE id=1"""
        )
    created = asyncio.run(main.upload_video(
        UploadFile(filename="recording.mp4", file=io.BytesIO(b"fake-video")), "default",
    ))
    with db.connection() as con:
        con.execute(
            """UPDATE analysis_audio_defaults
               SET mode='single', single_track=4, microphone_track=4,
                   game_track=4, use_all_sounds=1, use_game=0 WHERE id=1"""
        )

    captured: list[dict | None] = []
    monkeypatch.setattr(
        main,
        "analyse",
        lambda _video_id, _report, audio_snapshot=None: captured.append(audio_snapshot),
    )
    main.run_analysis(created["video_id"], created["job_id"], lambda *_args: None)

    assert captured == [{
        "mode": "split",
        "single_track": 1,
        "microphone_track": 2,
        "all_sounds_track": 1,
        "game_track": 3,
        "use_all_sounds": 0,
        "use_game": 1,
    }]


def test_source_removal_keeps_review_history_training_data_and_audio(
    api_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = settings.incoming_dir / "reviewed-stream.mp4"
    source.write_bytes(b"large-source-placeholder")
    timestamp = db.now()
    with db.connection() as con:
        con.execute(
            """INSERT INTO videos
               (id, original_name, path, source_size_bytes, status, created_at, updated_at)
               VALUES ('reviewed-video', 'reviewed-stream.mp4', ?, ?, 'ready', ?, ?)""",
            (str(source), source.stat().st_size, timestamp, timestamp),
        )
    run_id = start_analysis_run("reviewed-video", "default")
    persist_analysis_results(
        "reviewed-video",
        run_id,
        [
            {
                "id": "reviewed-moment",
                "start": 5.0,
                "end": 18.0,
                "text": "To jest kompletna i samodzielna reakcja na wydarzenie w grze.",
                "tags": ["reakcja na grę"],
                "vector": [1.0, 0.0],
                "quality_score": 78,
                "short_potential_score": 74,
            },
            {
                "id": "tag-only-moment",
                "start": 25.0,
                "end": 34.0,
                "text": "Ten fragment ma wyłącznie ręcznie sprawdzony tag.",
                "tags": ["opinia"],
                "vector": [0.0, 1.0],
                "quality_score": 61,
                "short_potential_score": 58,
            },
        ],
    )
    set_review("reviewed-moment", "accepted", profile="general")
    set_tag_verdict("reviewed-moment", "reakcja na grę", "correct")
    set_tag_verdict("tag-only-moment", "opinia", "correct")

    # A file from the old segment-id-only scheme must not suppress a fresh,
    # revision-specific archive.
    stale_archive = settings.review_audio_dir / "reviewed-moment.mp3"
    stale_archive.write_bytes(b"stale-audio")

    monkeypatch.setattr(main, "audio_track_count", lambda _source: 1)

    def fake_archive(
        _source: Path, destination: Path, _start: float, _end: float,
        _track: int, _pause_ranges,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"review-audio")

    monkeypatch.setattr(main, "export_audio_preview", fake_archive)

    result = main.delete_video("reviewed-video")

    assert result["ok"] is True and result["archived_segments"] == 2
    assert not source.exists()
    video = db.row("SELECT source_removed, source_size_bytes FROM videos WHERE id='reviewed-video'")
    assert video == {"source_removed": 1, "source_size_bytes": len(b"large-source-placeholder")}
    segment = db.row(
        "SELECT lifecycle_state, transcript, archive_audio_path FROM segments WHERE id='reviewed-moment'"
    )
    assert segment and segment["lifecycle_state"] == "current"
    assert segment["transcript"].startswith("To jest kompletna")
    assert Path(segment["archive_audio_path"]).read_bytes() == b"review-audio"
    assert Path(segment["archive_audio_path"]) != stale_archive
    tag_only = db.row("SELECT archive_audio_path FROM segments WHERE id='tag-only-moment'")
    assert tag_only and Path(tag_only["archive_audio_path"]).read_bytes() == b"review-audio"
    assert db.row("SELECT rating FROM segment_reviews WHERE segment_id='tag-only-moment'")["rating"] == "unrated"
    assert db.row("SELECT rating FROM segment_reviews WHERE segment_id='reviewed-moment'")["rating"] == "accepted"
    assert db.row("SELECT decision FROM preference_feedback WHERE segment_id='reviewed-moment'")["decision"] == "accepted"
    assert db.row("SELECT verdict FROM segment_tag_reviews WHERE segment_id='reviewed-moment'")["verdict"] == "correct"
    assert db.row("SELECT COUNT(*) AS value FROM segment_revisions WHERE segment_id='reviewed-moment'")["value"] == 1
    with pytest.raises(main.HTTPException) as error:
        main.restart_analysis("reviewed-video")
    assert error.value.status_code == 409


def test_missing_source_is_not_marked_removed_without_review_audio_archive(
    api_data_dir: Path,
) -> None:
    missing_source = settings.incoming_dir / "temporarily-unavailable.mp4"
    timestamp = db.now()
    with db.connection() as con:
        con.execute(
            """INSERT INTO videos
               (id, original_name, path, source_size_bytes, status, created_at, updated_at)
               VALUES ('missing-source-video', 'temporarily-unavailable.mp4', ?, 1234, 'ready', ?, ?)""",
            (str(missing_source), timestamp, timestamp),
        )
    run_id = start_analysis_run("missing-source-video", "default")
    persist_analysis_results(
        "missing-source-video",
        run_id,
        [{
            "id": "reviewed-without-archive",
            "start": 4.0,
            "end": 12.0,
            "text": "Ten oceniony fragment nadal wymaga zachowania dzwieku.",
            "tags": ["opinia"],
            "vector": [1.0, 0.0],
            "quality_score": 70,
            "short_potential_score": 68,
        }],
    )
    set_review("reviewed-without-archive", "accepted", profile="general")

    with pytest.raises(main.HTTPException) as error:
        main.delete_video("missing-source-video")

    assert error.value.status_code == 409
    assert "Restore the source file" in error.value.detail
    video = db.row(
        "SELECT source_removed, source_removed_at, source_size_bytes FROM videos WHERE id='missing-source-video'"
    )
    assert video == {"source_removed": 0, "source_removed_at": None, "source_size_bytes": 1234}
    assert db.row(
        "SELECT archive_audio_path FROM segments WHERE id='reviewed-without-archive'"
    )["archive_audio_path"] in {None, ""}


def test_optional_startup_backfill_failure_does_not_block_other_repairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_names = (
        "backfill_segment_quality", "backfill_reading_filter", "backfill_context_signals",
        "backfill_segment_context", "remove_legacy_game_audio_bonus", "backfill_moment_reactions",
        "backfill_duplicate_groups", "backfill_detailed_tags", "backfill_preference_feedback",
        "backfill_short_potential",
    )
    for name in callback_names:
        monkeypatch.setattr(main, name, lambda: None)

    def broken_backfill() -> None:
        raise ValueError("legacy row is malformed")

    monkeypatch.setattr(main, "backfill_reading_filter", broken_backfill)
    monkeypatch.setattr(main.db, "maintenance_task_completed", lambda _name: False)
    completed: list[str] = []
    monkeypatch.setattr(main.db, "mark_maintenance_task_completed", completed.append)
    monkeypatch.setattr(main.db, "rows", lambda *_args, **_kwargs: [])
    failures: list[str] = []
    monkeypatch.setattr(main.diagnostics, "log_failure", lambda message, _exc: failures.append(message))

    main.run_startup_maintenance()

    assert "reading-filter-v3" not in completed
    assert "short-potential-v1" in completed
    assert failures == ["Startup maintenance deferred: reading-filter-v3"]
