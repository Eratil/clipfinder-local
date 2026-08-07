from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import database as db
from app.config import settings
from app.services.analysis_store import (
    _projection,
    fail_running_analysis,
    persist_analysis_results,
    record_manual_revision_with_updates,
    start_analysis_run,
    sync_current_revision_snapshot,
    update_analysis_run_inputs,
    update_current_segment_and_revision,
)


@pytest.fixture
def history_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "clipfinder_data_dir", tmp_path)
    db.initialize()
    timestamp = db.now()
    with db.connection() as con:
        con.execute(
            """INSERT INTO videos
               (id, original_name, path, status, created_at, updated_at, analysis_mode)
               VALUES ('video', 'video.mp4', ?, 'ready', ?, ?, 'default')""",
            (str(tmp_path / "video.mp4"), timestamp, timestamp),
        )
        con.execute(
            """INSERT INTO segments
               (id, video_id, start_seconds, end_seconds, transcript, keywords, tags,
                word_timestamps, embedding, rating, review_reason, quality_score,
                analysis_run_id, revision_number, lifecycle_state, created_at)
               VALUES ('moment', 'video', 10, 25, 'To jest moja kompletna opinia o tej grze',
                       '[]', '["opinia"]', '[]', '[1,0]', 'accepted', '', 70,
                       '', 1, 'current', ?)""",
            (timestamp,),
        )
        # This fixture injects a legacy row after creating the current empty
        # schema.  A real legacy database has no completed migration marker.
        con.execute(
            "DELETE FROM maintenance_tasks WHERE name=?",
            (db.ANALYSIS_HISTORY_MIGRATION,),
        )
    # Run again to exercise the same legacy backfill used by real upgrades.
    db.initialize()
    yield


def _record(record_id: str, start: float, end: float, text: str) -> dict:
    return {
        "id": record_id, "start": start, "end": end, "text": text,
        "keywords": ["kompletna"], "tags": ["opinia"], "words": [],
        "vector": [1.0, 0.0], "quality_score": 72,
        "quality_signals": ["complete thought"], "short_potential_score": 68,
        "short_potential_signals": ["short-friendly length"],
        "reading_likelihood": 0.0, "logical_sense_score": 80,
        "context_score": 75, "self_contained_score": 82,
        "extended_completeness_score": 78, "context_before": "", "context_after": "",
        "audio_event_score": 0, "game_reaction_score": 0, "voice_expression_score": 4,
        "moment_reaction_score": 0, "moment_reaction_stage": "", "vision_score": 0,
        "duplicate_group": "",
    }


def test_reanalysis_keeps_stable_id_review_and_two_revisions(history_db):
    run_id = start_analysis_run("video", "default")
    result = persist_analysis_results(
        "video", run_id,
        [_record("temporary", 10.4, 25.2, "To jest moja kompletna opinia o tej grze")],
    )

    assert result["matched"] == 1
    segment = db.row("SELECT * FROM segments WHERE id='moment'")
    assert segment and segment["lifecycle_state"] == "current"
    assert segment["rating"] == "accepted"
    assert segment["revision_number"] == 2
    assert db.row("SELECT COUNT(*) AS value FROM segment_revisions WHERE segment_id='moment'")["value"] == 2
    review = db.row("SELECT * FROM segment_reviews WHERE segment_id='moment'")
    assert review and review["rating"] == "accepted"
    assert review["reviewed_revision_id"] == "legacy-revision:moment"
    assert db.row("SELECT id FROM analysis_runs WHERE id=? AND state='completed' AND is_current=1", (run_id,))


def test_unmatched_review_is_retired_not_deleted(history_db):
    run_id = start_analysis_run("video", "default")
    result = persist_analysis_results(
        "video", run_id,
        [_record("new-moment", 90, 104, "Zupełnie inny fragment transmisji")],
    )

    assert result["retired"] == 1
    old = db.row("SELECT * FROM segments WHERE id='moment'")
    assert old and old["lifecycle_state"] == "retired" and old["rating"] == "accepted"
    assert db.row("SELECT rating FROM segment_reviews WHERE segment_id='moment'")["rating"] == "accepted"
    assert db.row("SELECT lifecycle_state FROM segments WHERE id='new-moment'")["lifecycle_state"] == "current"


def test_reviewed_moment_disappears_then_reclaims_stable_id_when_it_returns(history_db):
    missing_run = start_analysis_run("video", "fast")
    persist_analysis_results(
        "video", missing_run,
        [_record("temporary-other", 90, 104, "Zupełnie inny fragment transmisji")],
    )
    assert db.row("SELECT lifecycle_state FROM segments WHERE id='moment'")["lifecycle_state"] == "retired"

    returning_run = start_analysis_run("video", "extended")
    result = persist_analysis_results(
        "video", returning_run,
        [_record("ephemeral-return", 10.2, 25.1, "To jest moja kompletna opinia o tej grze")],
    )

    assert result["matched"] == 1 and result["new"] == 0
    restored = db.row("SELECT lifecycle_state, rating, revision_number FROM segments WHERE id='moment'")
    assert restored == {"lifecycle_state": "current", "rating": "accepted", "revision_number": 2}
    assert db.row("SELECT id FROM segments WHERE id='ephemeral-return'") is None
    assert db.row("SELECT rating FROM segment_reviews WHERE segment_id='moment'")["rating"] == "accepted"
    assert db.row("SELECT COUNT(*) AS value FROM segment_revisions WHERE segment_id='moment'")["value"] == 2


def test_failed_staged_run_does_not_replace_current_run(history_db):
    previous = db.row("SELECT id FROM analysis_runs WHERE video_id='video' AND is_current=1")
    run_id = start_analysis_run("video", "extended")
    fail_running_analysis("video", "synthetic failure")

    assert db.row("SELECT id FROM analysis_runs WHERE video_id='video' AND is_current=1")["id"] == previous["id"]
    failed = db.row("SELECT state, is_current, error_message FROM analysis_runs WHERE id=?", (run_id,))
    assert failed == {"state": "failed", "is_current": 0, "error_message": "synthetic failure"}
    assert db.row("SELECT lifecycle_state FROM segments WHERE id='moment'")["lifecycle_state"] == "current"


def test_empty_reanalysis_is_failed_without_retiring_previous_candidates(history_db):
    previous_run = db.row(
        "SELECT id FROM analysis_runs WHERE video_id='video' AND is_current=1 AND state='completed'"
    )
    previous_revision_count = db.row(
        "SELECT COUNT(*) AS value FROM segment_revisions WHERE segment_id='moment'"
    )["value"]
    run_id = start_analysis_run("video", "extended")

    with pytest.raises(RuntimeError, match="previous analysis was preserved"):
        persist_analysis_results("video", run_id, [])

    assert db.row(
        "SELECT id FROM analysis_runs WHERE video_id='video' AND is_current=1 AND state='completed'"
    ) == previous_run
    assert db.row(
        "SELECT state, is_current, error_message FROM analysis_runs WHERE id=?",
        (run_id,),
    ) == {
        "state": "failed",
        "is_current": 0,
        "error_message": "Reanalysis produced no candidates; the previous analysis was preserved.",
    }
    assert db.row(
        "SELECT lifecycle_state, rating FROM segments WHERE id='moment'"
    ) == {"lifecycle_state": "current", "rating": "accepted"}
    assert db.row(
        "SELECT COUNT(*) AS value FROM segment_revisions WHERE segment_id='moment'"
    )["value"] == previous_revision_count


def test_resolved_runtime_and_audio_inputs_replace_initial_run_provenance(history_db):
    run_id = start_analysis_run("video", "default")

    update_analysis_run_inputs(
        run_id,
        whisper_model="small",
        whisper_device="cpu",
        whisper_compute_type="int8",
        transcript_audio_track=3,
        audio_analysis_mode="split",
    )

    assert db.row(
        """SELECT whisper_model, whisper_device, whisper_compute_type,
                  transcript_audio_track, audio_analysis_mode
           FROM analysis_runs WHERE id=?""",
        (run_id,),
    ) == {
        "whisper_model": "small",
        "whisper_device": "cpu",
        "whisper_compute_type": "int8",
        "transcript_audio_track": 3,
        "audio_analysis_mode": "split",
    }


def _revision_payload(segment_id: str, revision_number: int | None = None) -> tuple[dict, dict]:
    segment = db.row("SELECT * FROM segments WHERE id=?", (segment_id,))
    assert segment
    selected_revision = revision_number or int(segment["revision_number"])
    revision = db.row(
        "SELECT * FROM segment_revisions WHERE segment_id=? AND revision_number=?",
        (segment_id, selected_revision),
    )
    assert revision
    return segment, json.loads(revision["payload_json"])


def test_derived_update_changes_current_segment_and_same_revision_payload(history_db):
    before = db.row("SELECT id, revision_number FROM segment_revisions WHERE segment_id='moment' AND is_current=1")
    revision_id = update_current_segment_and_revision(
        "moment",
        {
            "chat_reaction_score": 17,
            "chat_message_count": 6,
            "tags": '["opinia","reakcja: czat"]',
        },
        expected_revision_number=1,
    )

    segment, payload = _revision_payload("moment")
    assert revision_id == before["id"]
    assert segment["revision_number"] == 1
    assert segment["chat_reaction_score"] == payload["chat_reaction_score"] == 17
    assert segment["chat_message_count"] == payload["chat_message_count"] == 6
    assert segment["tags"] == payload["tags"] == '["opinia","reakcja: czat"]'
    assert db.row("SELECT COUNT(*) AS value FROM segment_revisions WHERE segment_id='moment'")["value"] == 1


def test_snapshot_can_share_the_callers_transaction_after_bulk_derived_update(history_db):
    with db.connection() as con:
        con.execute("UPDATE segments SET duplicate_group='group-7' WHERE id='moment'")
        revision_id = sync_current_revision_snapshot(con, "moment", expected_revision_number=1)
        payload = json.loads(con.execute(
            "SELECT payload_json FROM segment_revisions WHERE id=?", (revision_id,)
        ).fetchone()["payload_json"])
        assert payload["duplicate_group"] == "group-7"
    _segment, committed_payload = _revision_payload("moment")
    assert committed_payload["duplicate_group"] == "group-7"


def test_invalid_derived_field_does_not_modify_segment_or_revision(history_db):
    _segment, before = _revision_payload("moment")
    with pytest.raises(ValueError, match="Unsupported segment machine fields"):
        update_current_segment_and_revision("moment", {"rating": "rejected", "quality_score": 5})
    segment, after = _revision_payload("moment")
    assert segment["quality_score"] == 70
    assert after == before


def test_revision_mismatch_rolls_back_atomic_derived_update(history_db):
    with pytest.raises(RuntimeError, match="changed from 99 to 1"):
        update_current_segment_and_revision(
            "moment", {"quality_score": 5}, expected_revision_number=99,
        )
    segment, payload = _revision_payload("moment")
    assert segment["quality_score"] == payload["quality_score"] == 70


def test_manual_update_creates_revision_and_keeps_review_on_reviewed_content(history_db):
    review_before = db.row("SELECT * FROM segment_reviews WHERE segment_id='moment'")
    old_revision_id = review_before["reviewed_revision_id"]

    new_revision_id = record_manual_revision_with_updates(
        "moment",
        {
            "start_seconds": 11.5,
            "end_seconds": 24.0,
            "transcript": "Ręcznie poprawiona, pełna opinia o tej grze",
            "quality_score": 81,
        },
        "timing_and_transcript_edit",
    )

    segment, payload = _revision_payload("moment", 2)
    review_after = db.row("SELECT * FROM segment_reviews WHERE segment_id='moment'")
    assert segment["revision_number"] == 2
    assert segment["start_seconds"] == payload["start_seconds"] == 11.5
    assert segment["transcript"] == payload["transcript"]
    assert new_revision_id != old_revision_id
    assert review_after["reviewed_revision_id"] == old_revision_id
    assert review_after["rating"] == "accepted"
    serialised = db.serialize_segment(segment)
    assert serialised["review_stale"] is True
    revisions = db.rows(
        "SELECT revision_number, is_current FROM segment_revisions WHERE segment_id='moment' ORDER BY revision_number"
    )
    assert revisions == [
        {"revision_number": 1, "is_current": 0},
        {"revision_number": 2, "is_current": 1},
    ]


def test_pipeline_projection_covers_every_machine_column():
    projection = _projection({
        "start": 1.0,
        "end": 8.0,
        "text": "PeĹ‚na wypowiedĹş testowa.",
        "words": [],
        "vector": [0.1, 0.2],
    })
    assert set(projection) == set(db.SEGMENT_MACHINE_COLUMNS)
