from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from sqlite3 import Connection
from typing import Any

from app import database as db
from app.config import settings
from app.services.analysis_history import (
    MOMENT_MATCHER_VERSION,
    PIPELINE_VERSION,
    SCORING_VERSION,
    TAGGING_VERSION,
    has_human_data,
    match_moments,
)


def start_analysis_run(video_id: str, analysis_mode: str) -> str:
    """Create a staged run without replacing the last successful run."""
    run_id = str(uuid.uuid4())
    timestamp = db.now()
    with db.connection() as con:
        parent = con.execute(
            "SELECT id FROM analysis_runs WHERE video_id=? AND is_current=1 ORDER BY sequence DESC LIMIT 1",
            (video_id,),
        ).fetchone()
        sequence = int(con.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM analysis_runs WHERE video_id=?",
            (video_id,),
        ).fetchone()["value"])
        video = con.execute(
            "SELECT transcript_audio_track, audio_analysis_mode FROM videos WHERE id=?",
            (video_id,),
        ).fetchone()
        con.execute(
            """INSERT INTO analysis_runs
               (id, video_id, parent_run_id, sequence, state, is_current, analysis_mode,
                pipeline_version, scoring_version, tagging_version, matcher_version,
                whisper_model, whisper_device, whisper_compute_type, transcript_audio_track,
                audio_analysis_mode, started_at, created_at)
               VALUES (?, ?, ?, ?, 'running', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, video_id, parent["id"] if parent else None, sequence, analysis_mode,
                PIPELINE_VERSION, SCORING_VERSION, TAGGING_VERSION, MOMENT_MATCHER_VERSION,
                settings.whisper_model, settings.whisper_device, settings.whisper_compute_type,
                int(video["transcript_audio_track"] or 1) if video else 1,
                str(video["audio_analysis_mode"] or "single") if video else "single",
                timestamp, timestamp,
            ),
        )
    return run_id


def fail_running_analysis(video_id: str, error: str) -> None:
    """Mark staged runs failed; the previous current run is left untouched."""
    with db.connection() as con:
        con.execute(
            """UPDATE analysis_runs SET state='failed', is_current=0, error_message=?, completed_at=?
               WHERE video_id=? AND state='running'""",
            (str(error)[:4000], db.now(), video_id),
        )


def set_latest_run_elapsed(video_id: str, elapsed_seconds: float) -> None:
    with db.connection() as con:
        con.execute(
            """UPDATE analysis_runs SET elapsed_seconds=?
               WHERE id=(SELECT id FROM analysis_runs WHERE video_id=? ORDER BY sequence DESC LIMIT 1)""",
            (round(max(0.0, elapsed_seconds), 2), video_id),
        )


def update_analysis_run_inputs(
    run_id: str,
    *,
    whisper_model: str,
    whisper_device: str,
    whisper_compute_type: str,
    transcript_audio_track: int,
    audio_analysis_mode: str,
) -> None:
    """Record resolved inputs after track validation and runtime fallback."""
    with db.connection() as con:
        con.execute(
            """UPDATE analysis_runs
               SET whisper_model=?, whisper_device=?, whisper_compute_type=?,
                   transcript_audio_track=?, audio_analysis_mode=?
               WHERE id=?""",
            (
                whisper_model,
                whisper_device,
                whisper_compute_type,
                int(transcript_audio_track),
                audio_analysis_mode,
                run_id,
            ),
        )


def _projection(record: dict[str, Any]) -> dict[str, Any]:
    """Translate an in-memory pipeline candidate to the segments projection."""
    return {
        "start_seconds": float(record["start"]),
        "end_seconds": float(record["end"]),
        "transcript": str(record.get("text") or ""),
        "keywords": json.dumps(record.get("keywords") or [], ensure_ascii=False),
        "tags": json.dumps(record.get("tags") or [], ensure_ascii=False),
        "word_timestamps": json.dumps(record.get("words") or [], ensure_ascii=False),
        "embedding": json.dumps(record.get("vector")) if record.get("vector") is not None else None,
        "quality_score": int(record.get("quality_score") or 0),
        "quality_signals": json.dumps(record.get("quality_signals") or [], ensure_ascii=False),
        "short_potential_score": int(record.get("short_potential_score") if record.get("short_potential_score") is not None else -1),
        "short_potential_signals": json.dumps(record.get("short_potential_signals") or [], ensure_ascii=False),
        "reading_likelihood": float(record.get("reading_likelihood") or 0),
        "text_reading_likelihood": float(record.get("text_reading_likelihood") or 0),
        "visual_reading_likelihood": float(record.get("visual_reading_likelihood") or 0),
        "extended_reading_likelihood": float(record.get("extended_reading_likelihood") or 0),
        "extended_hook_score": int(record.get("extended_hook_score") if record.get("extended_hook_score") is not None else -1),
        "extended_ending_score": int(record.get("extended_ending_score") if record.get("extended_ending_score") is not None else -1),
        "extended_story_signals": json.dumps(record.get("extended_story_signals") or [], ensure_ascii=False),
        "boundary_signals": json.dumps(record.get("boundary_signals") or [], ensure_ascii=False),
        "context_signals": json.dumps(record.get("context_signals") or [], ensure_ascii=False),
        "audio_event_score": int(record.get("audio_event_score") or 0),
        "game_reaction_score": int(record.get("game_reaction_score") or 0),
        "voice_expression_score": int(record.get("voice_expression_score") or 0),
        "moment_reaction_score": int(record.get("moment_reaction_score") or 0),
        "moment_reaction_stage": str(record.get("moment_reaction_stage") or ""),
        "vision_score": int(record.get("vision_score") or 0),
        "chat_reaction_score": 0,
        "chat_joy_score": 0,
        "chat_message_count": 0,
        "chat_unique_authors": 0,
        "chat_surge": 0.0,
        "chat_messages": "[]",
        "duplicate_group": str(record.get("duplicate_group") or ""),
        "logical_sense_score": int(record.get("logical_sense_score") if record.get("logical_sense_score") is not None else -1),
        "context_score": int(record.get("context_score") if record.get("context_score") is not None else -1),
        "self_contained_score": int(record.get("self_contained_score") if record.get("self_contained_score") is not None else -1),
        "extended_completeness_score": int(record.get("extended_completeness_score") if record.get("extended_completeness_score") is not None else -1),
        "chat_question_match_score": 0,
        "chat_question_text": "",
        "context_before": str(record.get("context_before") or ""),
        "context_after": str(record.get("context_after") or ""),
    }


def _insert_revision(
    con,
    segment: dict[str, Any],
    run_id: str,
    revision_number: int,
    *,
    kind: str,
    confidence: float | None = None,
    reason: str = "",
) -> str:
    revision_id = str(uuid.uuid4())
    con.execute("UPDATE segment_revisions SET is_current=0 WHERE segment_id=? AND is_current=1", (segment["id"],))
    con.execute(
        """INSERT INTO segment_revisions
           (id, segment_id, analysis_run_id, revision_number, revision_kind, is_current,
            start_seconds, end_seconds, transcript, embedding, payload_json,
            match_confidence, match_reason, created_at)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            revision_id, segment["id"], run_id, revision_number, kind,
            segment["start_seconds"], segment["end_seconds"], segment.get("transcript") or "",
            segment.get("embedding"),
            json.dumps(db.segment_machine_payload(segment), ensure_ascii=False, separators=(",", ":")),
            confidence, reason, db.now(),
        ),
    )
    return revision_id


def _machine_updates(values: Mapping[str, Any]) -> dict[str, Any]:
    """Validate fields which may be mirrored into an analysis revision.

    Review decisions intentionally cannot pass through this helper.  They live
    in ``segment_reviews`` and must never become part of a machine snapshot.
    """
    updates = dict(values)
    unsupported = sorted(set(updates) - set(db.SEGMENT_MACHINE_COLUMNS))
    if unsupported:
        raise ValueError(f"Unsupported segment machine fields: {', '.join(unsupported)}")
    return updates


def sync_current_revision_snapshot(
    con: Connection,
    segment_id: str,
    *,
    expected_revision_number: int | None = None,
) -> str:
    """Mirror the current segment projection into its current revision.

    Call this inside the same database transaction as a derived update.  It
    does not create a new revision: chat scores, duplicate groups and other
    recomputed features are all part of the analysis revision that produced
    the current segment.  A mismatch is rejected rather than silently writing
    a payload into the wrong revision.
    """
    row = con.execute(
        "SELECT * FROM segments WHERE id=? AND lifecycle_state='current'",
        (segment_id,),
    ).fetchone()
    if not row:
        raise ValueError("Current segment not found")
    segment = dict(row)
    revision_number = int(segment.get("revision_number") or 1)
    if expected_revision_number is not None and revision_number != int(expected_revision_number):
        raise RuntimeError(
            f"Segment revision changed from {int(expected_revision_number)} to {revision_number} during update"
        )
    revision = con.execute(
        """SELECT id, revision_number FROM segment_revisions
           WHERE segment_id=? AND is_current=1""",
        (segment_id,),
    ).fetchone()
    if not revision:
        raise RuntimeError("Current segment revision is missing")
    if int(revision["revision_number"]) != revision_number:
        raise RuntimeError(
            "Current segment and revision numbers differ; refusing to overwrite revision history"
        )
    con.execute(
        """UPDATE segment_revisions
              SET start_seconds=?, end_seconds=?, transcript=?, embedding=?, payload_json=?
            WHERE id=?""",
        (
            segment["start_seconds"], segment["end_seconds"], segment.get("transcript") or "",
            segment.get("embedding"),
            json.dumps(db.segment_machine_payload(segment), ensure_ascii=False, separators=(",", ":")),
            revision["id"],
        ),
    )
    return str(revision["id"])


def update_current_segment_and_revision(
    segment_id: str,
    values: Mapping[str, Any],
    *,
    con: Connection | None = None,
    expected_revision_number: int | None = None,
) -> str:
    """Atomically update derived machine fields and their revision payload.

    Supplying ``con`` lets a caller include other writes in the same
    transaction.  Without it this function owns and commits one transaction.
    The returned value is the unchanged current revision ID.
    """
    updates = _machine_updates(values)
    manager = nullcontext(con) if con is not None else db.connection()
    with manager as active:
        assert active is not None
        if not active.execute(
            "SELECT id FROM segments WHERE id=? AND lifecycle_state='current'",
            (segment_id,),
        ).fetchone():
            raise ValueError("Current segment not found")
        if updates:
            assignments = ", ".join(f"{column}=?" for column in updates)
            active.execute(
                f"UPDATE segments SET {assignments} WHERE id=? AND lifecycle_state='current'",
                (*updates.values(), segment_id),
            )
        return sync_current_revision_snapshot(
            active,
            segment_id,
            expected_revision_number=expected_revision_number,
        )


def record_manual_revision_with_updates(
    segment_id: str,
    values: Mapping[str, Any],
    kind: str,
) -> str:
    """Apply a manual edit and create its new immutable revision atomically.

    ``segment_reviews.reviewed_revision_id`` is deliberately not advanced.
    The user reviewed the previous content, so an accepted/rejected clip becomes
    stale until it is explicitly reviewed again after changing timing or text.
    """
    updates = _machine_updates(values)
    if not str(kind or "").strip():
        raise ValueError("Manual revision kind is required")
    with db.connection() as con:
        row = con.execute(
            "SELECT * FROM segments WHERE id=? AND lifecycle_state='current'",
            (segment_id,),
        ).fetchone()
        if not row:
            raise ValueError("Current segment not found")
        if updates:
            assignments = ", ".join(f"{column}=?" for column in updates)
            con.execute(
                f"UPDATE segments SET {assignments} WHERE id=? AND lifecycle_state='current'",
                (*updates.values(), segment_id),
            )
        segment = dict(con.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone())
        revision_number = max(1, int(segment.get("revision_number") or 1)) + 1
        con.execute("UPDATE segments SET revision_number=? WHERE id=?", (revision_number, segment_id))
        segment["revision_number"] = revision_number
        return _insert_revision(
            con,
            segment,
            str(segment.get("analysis_run_id") or ""),
            revision_number,
            kind=str(kind).strip(),
            reason="manual edit",
        )


def persist_analysis_results(video_id: str, run_id: str, records: list[dict[str, Any]]) -> dict[str, int]:
    """Atomically activate a successful run without deleting previous moments."""
    previous = db.rows(
        "SELECT * FROM segments WHERE video_id=? AND lifecycle_state='current' ORDER BY start_seconds, id",
        (video_id,),
    )
    if not records and previous:
        # Zero candidates can be a valid first analysis of a silent recording,
        # but replacing an existing non-empty result with nothing is much more
        # likely to be a pipeline/runtime regression.  Fail this staged run and
        # leave the previous current run and moments untouched.
        anomaly = "Reanalysis produced no candidates; the previous analysis was preserved."
        rejected = False
        with db.connection() as con:
            run = con.execute(
                "SELECT state, parent_run_id FROM analysis_runs WHERE id=? AND video_id=?",
                (run_id, video_id),
            ).fetchone()
            if run and run["state"] == "running" and run["parent_run_id"]:
                con.execute(
                    """UPDATE analysis_runs
                       SET state='failed', is_current=0, error_message=?, completed_at=?
                       WHERE id=?""",
                    (anomaly, db.now(), run_id),
                )
                rejected = True
        if rejected:
            raise RuntimeError(anomaly)
    match_result = match_moments(previous, records)
    by_new_id = match_result.by_new_id()
    previous_by_id = {str(item["id"]): item for item in previous}

    # A reviewed moment may briefly disappear when a different model/mode
    # proposes another candidate set, then return in a later reanalysis. Give
    # such retired human data one conservative chance to reclaim its stable ID.
    # Current moments always win; retired candidates require high confidence
    # and strong transcript agreement to prevent a neighbouring utterance from
    # inheriting an old review.
    unmatched_new_ids = set(match_result.unmatched_new_ids)
    unmatched_records = [
        record for record in records if str(record["id"]) in unmatched_new_ids
    ]
    retired_candidates = db.rows(
        """SELECT s.*,
                  EXISTS(SELECT 1 FROM segment_tag_reviews tr WHERE tr.segment_id=s.id) AS has_tag_review,
                  EXISTS(SELECT 1 FROM collection_examples ce WHERE ce.segment_id=s.id) AS has_collection_example,
                  EXISTS(SELECT 1 FROM preference_feedback pf WHERE pf.segment_id=s.id) AS has_preference_feedback,
                  EXISTS(SELECT 1 FROM segment_revisions sr
                         WHERE sr.segment_id=s.id
                           AND sr.revision_kind NOT IN ('analysis','reanalysis','legacy')) AS has_manual_revision
           FROM segments s
           WHERE s.video_id=? AND s.lifecycle_state='retired'
           ORDER BY s.start_seconds, s.id""",
        (video_id,),
    )
    retired_with_human_data = [
        item for item in retired_candidates
        if has_human_data(item)
        or any(bool(item.get(key)) for key in (
            "has_tag_review", "has_collection_example", "has_preference_feedback", "has_manual_revision",
        ))
    ]
    if unmatched_records and retired_with_human_data:
        retired_result = match_moments(retired_with_human_data, unmatched_records)
        restored_matches = [
            match for match in retired_result.matches
            if match.confidence == "high" and match.signals.text_similarity >= 0.78
        ]
        by_new_id.update({match.new_id: match for match in restored_matches})
        previous_by_id.update({str(item["id"]): item for item in retired_with_human_data})
    timestamp = db.now()
    columns = list(db.SEGMENT_MACHINE_COLUMNS)
    update_sql = ", ".join(f"{column}=?" for column in columns)

    with db.connection() as con:
        run = con.execute("SELECT state FROM analysis_runs WHERE id=? AND video_id=?", (run_id, video_id)).fetchone()
        if not run or run["state"] != "running":
            raise RuntimeError("The staged analysis run is missing or no longer running.")

        activated_ids: list[str] = []
        restored_reviews = 0
        for record in records:
            ephemeral_id = str(record["id"])
            match = by_new_id.get(ephemeral_id)
            stable_id = match.previous_id if match else ephemeral_id
            projection = _projection(record)
            if match:
                previous_segment = previous_by_id[stable_id]
                revision_number = max(1, int(previous_segment.get("revision_number") or 1)) + 1
                con.execute(
                    f"""UPDATE segments SET {update_sql}, analysis_run_id=?, revision_number=?,
                           lifecycle_state='current', retired_at=NULL WHERE id=?""",
                    tuple(projection[column] for column in columns) + (run_id, revision_number, stable_id),
                )
                if has_human_data(previous_segment):
                    restored_reviews += 1
            else:
                revision_number = 1
                insert_columns = ["id", "video_id", *columns, "analysis_run_id", "revision_number", "lifecycle_state", "retired_at", "created_at"]
                placeholders = ",".join("?" for _ in insert_columns)
                con.execute(
                    f"INSERT INTO segments ({', '.join(insert_columns)}) VALUES ({placeholders})",
                    (stable_id, video_id, *(projection[column] for column in columns), run_id, revision_number, "current", None, timestamp),
                )
            activated_ids.append(stable_id)
            current = dict(con.execute("SELECT * FROM segments WHERE id=?", (stable_id,)).fetchone())
            revision_id = _insert_revision(
                con, current, run_id, revision_number,
                kind="reanalysis" if match else "analysis",
                confidence=match.score if match else None,
                reason=match.reason if match else "new moment",
            )
            con.execute(
                """INSERT OR IGNORE INTO segment_reviews
                   (segment_id, reviewed_revision_id, rating, review_reason, censor_profanity,
                    remove_pauses, archive_audio_path, archive_audio_track, created_at, updated_at)
                   VALUES (?, ?, 'unrated', '', 0, 0, '', 1, ?, ?)""",
                (stable_id, revision_id, timestamp, timestamp),
            )

        for old_id in match_result.unmatched_previous_ids:
            con.execute(
                "UPDATE segments SET lifecycle_state='retired', retired_at=? WHERE id=?",
                (timestamp, old_id),
            )

        con.execute("UPDATE analysis_runs SET is_current=0 WHERE video_id=? AND is_current=1", (video_id,))
        con.execute(
            """UPDATE analysis_runs SET state='completed', is_current=1, candidate_count=?,
                      completed_at=?, error_message='' WHERE id=?""",
            (len(activated_ids), timestamp, run_id),
        )
        con.execute("UPDATE videos SET status='ready', error_message=NULL, updated_at=? WHERE id=?", (timestamp, video_id))

    return {
        "matched": len(by_new_id),
        "new": len(records) - len(by_new_id),
        "retired": len(match_result.unmatched_previous_ids),
        "reviewed_matches": restored_reviews,
    }


def refresh_current_revision_snapshots(video_id: str, run_id: str) -> None:
    """Capture chat-derived fields after chat scoring, without another revision."""
    with db.connection() as con:
        rows = con.execute(
            "SELECT id, revision_number FROM segments WHERE video_id=? AND analysis_run_id=? AND lifecycle_state='current'",
            (video_id, run_id),
        ).fetchall()
        for segment in rows:
            sync_current_revision_snapshot(
                con,
                str(segment["id"]),
                expected_revision_number=int(segment["revision_number"] or 1),
            )


def record_manual_revision(segment_id: str, kind: str) -> str:
    """Snapshot a timing/transcript edit while retaining the stable public ID."""
    return record_manual_revision_with_updates(segment_id, {}, kind)
