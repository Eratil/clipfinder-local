from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import database as db
from app.config import settings
from app.services import feedback


@pytest.fixture
def feedback_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "clipfinder_data_dir", tmp_path)
    db.initialize()
    timestamp = db.now()
    with db.connection() as con:
        con.execute(
            """INSERT INTO videos
               (id, original_name, path, status, created_at, updated_at)
               VALUES ('video', 'video.mp4', ?, 'ready', ?, ?)""",
            (str(tmp_path / "video.mp4"), timestamp, timestamp),
        )
        con.execute(
            """INSERT INTO analysis_runs
               (id, video_id, sequence, state, is_current, started_at, completed_at, created_at)
               VALUES ('run', 'video', 1, 'completed', 1, ?, ?, ?)""",
            (timestamp, timestamp, timestamp),
        )
        con.execute(
            """INSERT INTO segments
               (id, video_id, start_seconds, end_seconds, transcript, keywords,
                tags, word_timestamps, embedding, rating, review_reason,
                quality_score, logical_sense_score, context_score,
                self_contained_score, analysis_run_id, revision_number,
                lifecycle_state, created_at)
               VALUES ('segment', 'video', 10, 25,
                       'To jest kompletna opinia o tej grze', '[]',
                       '["humor","opinia"]', '[]', '[1.0,0.0]',
                       'unrated', '', 80, 78, 75, 82, 'run', 1, 'current', ?)""",
            (timestamp,),
        )
        segment = dict(con.execute("SELECT * FROM segments WHERE id='segment'").fetchone())
        con.execute(
            """INSERT INTO segment_revisions
               (id, segment_id, analysis_run_id, revision_number, revision_kind,
                is_current, start_seconds, end_seconds, transcript, embedding,
                payload_json, created_at)
               VALUES ('revision-1', 'segment', 'run', 1, 'analysis', 1,
                       10, 25, ?, ?, ?, ?)""",
            (
                segment["transcript"], segment["embedding"],
                json.dumps(db.segment_machine_payload(segment), ensure_ascii=False),
                timestamp,
            ),
        )
        con.execute(
            """INSERT INTO segment_reviews
               (segment_id, reviewed_revision_id, rating, review_reason,
                created_at, updated_at)
               VALUES ('segment', 'revision-1', 'unrated', '', ?, ?)""",
            (timestamp, timestamp),
        )
    yield


def _feedback(profile: str) -> dict | None:
    return db.row(
        "SELECT * FROM preference_feedback WHERE segment_id='segment' AND profile=?",
        (profile,),
    )


def _install_revision(revision_number: int, quality_score: int) -> None:
    revision_id = f"revision-{revision_number}"
    timestamp = db.now()
    with db.connection() as con:
        con.execute("UPDATE segment_revisions SET is_current=0 WHERE segment_id='segment'")
        con.execute(
            "UPDATE segments SET revision_number=?, quality_score=? WHERE id='segment'",
            (revision_number, quality_score),
        )
        segment = dict(con.execute("SELECT * FROM segments WHERE id='segment'").fetchone())
        con.execute(
            """INSERT INTO segment_revisions
               (id, segment_id, analysis_run_id, revision_number, revision_kind,
                is_current, start_seconds, end_seconds, transcript, embedding,
                payload_json, created_at)
               VALUES (?, 'segment', 'run', ?, 'reanalysis', 1, ?, ?, ?, ?, ?, ?)""",
            (
                revision_id, revision_number, segment["start_seconds"],
                segment["end_seconds"], segment["transcript"], segment["embedding"],
                json.dumps(db.segment_machine_payload(segment), ensure_ascii=False),
                timestamp,
            ),
        )


def test_accepted_review_updates_canonical_mirror_and_revision_snapshot(feedback_db):
    result = feedback.set_review("segment", "accepted", "ignored", "general")

    review = db.row("SELECT * FROM segment_reviews WHERE segment_id='segment'")
    segment = db.row("SELECT rating, review_reason FROM segments WHERE id='segment'")
    snapshot = _feedback("general")
    assert result["reviewed_revision_number"] == 1
    assert review["rating"] == segment["rating"] == "accepted"
    assert review["review_reason"] == segment["review_reason"] == ""
    assert review["reviewed_revision_id"] == "revision-1"
    assert snapshot["decision"] == "accepted"
    assert snapshot["reviewed_revision_number"] == 1
    assert json.loads(snapshot["features"])["values"]["quality"] == pytest.approx(80 / 99)


def test_rejected_review_normalizes_reason_and_snapshots_it(feedback_db):
    feedback.set_review("segment", "rejected", "  za   długi   fragment  ", "general")

    review = db.row("SELECT rating, review_reason FROM segment_reviews WHERE segment_id='segment'")
    segment = db.row("SELECT rating, review_reason FROM segments WHERE id='segment'")
    snapshot = _feedback("general")
    assert review == segment == {"rating": "rejected", "review_reason": "za długi fragment"}
    assert snapshot["decision"] == "rejected"
    assert snapshot["review_reason"] == "za długi fragment"
    assert db.row("SELECT reason FROM rejection_reasons WHERE reason='za długi fragment'")


def test_unrated_clears_only_the_selected_profile_snapshot(feedback_db):
    feedback.set_review("segment", "accepted", profile="general")
    feedback.set_review("segment", "rejected", "nie pasuje do horroru", "horror")

    assert _feedback("general")["decision"] == "accepted"
    assert _feedback("horror")["decision"] == "rejected"
    feedback.set_review("segment", "unrated", "must be ignored", "horror")

    assert _feedback("horror") is None
    assert _feedback("general")["decision"] == "accepted"
    assert db.row("SELECT rating, review_reason FROM segment_reviews WHERE segment_id='segment'") == {
        "rating": "unrated", "review_reason": "",
    }
    assert db.row("SELECT rating, review_reason FROM segments WHERE id='segment'") == {
        "rating": "unrated", "review_reason": "",
    }


def test_stale_review_never_refreshes_snapshot_with_new_revision(feedback_db):
    feedback.set_review("segment", "accepted", profile="general")
    before = _feedback("general")
    _install_revision(2, 12)

    assert feedback.refresh_training_snapshot_if_current("segment") == 0
    after = _feedback("general")
    assert after["reviewed_revision_number"] == 1
    assert after["features"] == before["features"]
    assert json.loads(after["features"])["values"]["quality"] == pytest.approx(80 / 99)
    assert db.row("SELECT reviewed_revision_id FROM segment_reviews WHERE segment_id='segment'") == {
        "reviewed_revision_id": "revision-1",
    }


def test_current_review_refreshes_only_matching_revision_profiles(feedback_db):
    feedback.set_review("segment", "accepted", profile="general")
    feedback.set_review("segment", "accepted", profile="soulslike")
    timestamp = db.now()
    with db.connection() as con:
        con.execute("UPDATE segments SET quality_score=45 WHERE id='segment'")
        segment = dict(con.execute("SELECT * FROM segments WHERE id='segment'").fetchone())
        con.execute(
            "UPDATE segment_revisions SET payload_json=? WHERE id='revision-1'",
            (json.dumps(db.segment_machine_payload(segment), ensure_ascii=False),),
        )
        # A historic profile snapshot must not be overwritten by this revision.
        con.execute(
            "UPDATE preference_feedback SET reviewed_revision_number=0, updated_at=? WHERE profile='soulslike'",
            (timestamp,),
        )

    assert feedback.refresh_training_snapshot_if_current("segment") == 1
    assert json.loads(_feedback("general")["features"])["values"]["quality"] == pytest.approx(45 / 99)
    assert json.loads(_feedback("soulslike")["features"])["values"]["quality"] == pytest.approx(80 / 99)


def test_invalid_rating_is_rejected_before_any_write(feedback_db):
    with pytest.raises(ValueError, match="Invalid rating"):
        feedback.set_review("segment", "approved", profile="general")

    assert db.row("SELECT rating FROM segment_reviews WHERE segment_id='segment'") == {"rating": "unrated"}
    assert _feedback("general") is None


def test_tag_verdict_validates_assignment_and_supports_unmarked(feedback_db):
    correct = feedback.set_tag_verdict("segment", " humor ", "correct")
    stored = db.row("SELECT tag, verdict FROM tag_feedback WHERE segment_id='segment'")
    assert stored == {"tag": correct["tag"], "verdict": "correct"}
    history = db.row(
        """SELECT reviewed_revision_id, canonical_tag, verdict, tagging_version
           FROM segment_tag_reviews WHERE segment_id='segment'"""
    )
    assert history == {
        "reviewed_revision_id": "revision-1",
        "canonical_tag": correct["tag"],
        "verdict": "correct",
        "tagging_version": "legacy",
    }

    feedback.set_tag_verdict("segment", "humor", "incorrect")
    assert db.row("SELECT verdict FROM tag_feedback WHERE segment_id='segment'") == {
        "verdict": "incorrect",
    }
    feedback.set_tag_verdict("segment", "humor", "unmarked")
    assert db.row("SELECT * FROM tag_feedback WHERE segment_id='segment'") is None
    assert db.row("SELECT * FROM segment_tag_reviews WHERE segment_id='segment'") is None

    with pytest.raises(ValueError, match="Invalid tag verdict"):
        feedback.set_tag_verdict("segment", "humor", "maybe")
    with pytest.raises(ValueError, match="no longer assigned"):
        feedback.set_tag_verdict("segment", "nonexistent", "correct")
