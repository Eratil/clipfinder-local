from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import database


LEGACY_CREATED_AT = "2026-08-01T18:00:00+00:00"


def _create_legacy_database(db_path: Path) -> None:
    """Create the relevant part of the schema used before analysis history v1."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(
            """
            CREATE TABLE videos (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                path TEXT NOT NULL,
                source_url TEXT,
                duration_seconds REAL,
                analysis_seconds REAL NOT NULL DEFAULT 0,
                analysis_mode TEXT NOT NULL DEFAULT 'default',
                status TEXT NOT NULL,
                error_message TEXT,
                transcript_audio_track INTEGER NOT NULL DEFAULT 1,
                audio_analysis_mode TEXT NOT NULL DEFAULT 'single',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                transcript TEXT NOT NULL DEFAULT '',
                keywords TEXT NOT NULL DEFAULT '[]',
                tags TEXT NOT NULL DEFAULT '[]',
                word_timestamps TEXT NOT NULL DEFAULT '[]',
                embedding TEXT,
                rating TEXT NOT NULL DEFAULT 'unrated',
                review_reason TEXT NOT NULL DEFAULT '',
                quality_score INTEGER NOT NULL DEFAULT 0,
                quality_signals TEXT NOT NULL DEFAULT '[]',
                short_potential_score INTEGER NOT NULL DEFAULT -1,
                short_potential_signals TEXT NOT NULL DEFAULT '[]',
                reading_likelihood REAL NOT NULL DEFAULT 0,
                censor_profanity INTEGER NOT NULL DEFAULT 0,
                remove_pauses INTEGER NOT NULL DEFAULT 0,
                archive_audio_path TEXT NOT NULL DEFAULT '',
                archive_audio_track INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE INDEX idx_segments_video_time ON segments(video_id, start_seconds);
            CREATE TABLE collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE collection_examples (
                collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY(collection_id, segment_id)
            );
            CREATE TABLE preference_feedback (
                id TEXT PRIMARY KEY,
                segment_id TEXT NOT NULL,
                profile TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('accepted', 'rejected')),
                review_reason TEXT NOT NULL DEFAULT '',
                embedding TEXT NOT NULL,
                features TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(segment_id, profile)
            );
            CREATE TABLE tag_feedback (
                segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                verdict TEXT NOT NULL CHECK(verdict IN ('correct', 'incorrect')),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(segment_id, tag)
            );
            """
        )
        con.execute(
            """INSERT INTO videos
               (id, original_name, path, duration_seconds, analysis_seconds, analysis_mode,
                status, transcript_audio_track, audio_analysis_mode, created_at, updated_at)
               VALUES ('video-legacy', 'stream.mp4', 'incoming/stream.mp4', 3600, 81.5,
                       'extended', 'ready', 2, 'split', ?, ?)""",
            (LEGACY_CREATED_AT, LEGACY_CREATED_AT),
        )
        con.executemany(
            """INSERT INTO segments
               (id, video_id, start_seconds, end_seconds, transcript, keywords, tags,
                word_timestamps, embedding, rating, review_reason, quality_score,
                quality_signals, short_potential_score, short_potential_signals,
                reading_likelihood, censor_profanity, remove_pauses, archive_audio_path,
                archive_audio_track, created_at)
               VALUES (?, 'video-legacy', ?, ?, ?, '[]', ?, '[]', ?, ?, ?, ?, '[]', ?,
                       '[]', ?, ?, ?, ?, ?, ?)""",
            [
                (
                    "segment-reviewed",
                    10.0,
                    26.5,
                    "To jest zachowany, zaakceptowany fragment.",
                    '["humor","reakcja na grę"]',
                    "[0.1,0.2,0.3]",
                    "accepted",
                    "",
                    78,
                    74,
                    0.05,
                    1,
                    1,
                    "review-audio/segment-reviewed.m4a",
                    2,
                    LEGACY_CREATED_AT,
                ),
                (
                    "segment-rejected",
                    80.0,
                    101.0,
                    "Ten fragment został odrzucony jako czytanie notatki.",
                    '["czytanie"]',
                    "[0.3,0.2,0.1]",
                    "rejected",
                    "czytanie notatki z gry",
                    29,
                    18,
                    0.94,
                    0,
                    0,
                    "",
                    1,
                    LEGACY_CREATED_AT,
                ),
            ],
        )
        con.execute(
            "INSERT INTO collections (id, name, created_at) VALUES ('collection-best', 'Najlepsze', ?)",
            (LEGACY_CREATED_AT,),
        )
        con.execute(
            """INSERT INTO collection_examples (collection_id, segment_id, created_at)
               VALUES ('collection-best', 'segment-reviewed', ?)""",
            (LEGACY_CREATED_AT,),
        )
        con.executemany(
            """INSERT INTO tag_feedback (segment_id, tag, verdict, updated_at)
               VALUES ('segment-reviewed', ?, ?, ?)""",
            [
                ("humor", "correct", LEGACY_CREATED_AT),
                ("czytanie", "incorrect", LEGACY_CREATED_AT),
            ],
        )
        con.execute(
            """INSERT INTO preference_feedback
               (id, segment_id, profile, decision, review_reason, embedding, features,
                created_at, updated_at)
               VALUES ('feedback-1', 'segment-reviewed', 'general', 'accepted', '',
                       '[0.1,0.2,0.3]', '{"quality":78}', ?, ?)""",
            (LEGACY_CREATED_AT, LEGACY_CREATED_AT),
        )


@pytest.fixture
def legacy_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "clipfinder-data"
    monkeypatch.setattr(database.settings, "clipfinder_data_dir", data_dir)
    _create_legacy_database(database.settings.db_path)
    return data_dir


def _table_counts(con: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "videos",
        "segments",
        "analysis_runs",
        "segment_revisions",
        "segment_reviews",
        "collections",
        "collection_examples",
        "tag_feedback",
        "segment_tag_reviews",
        "preference_feedback",
    )
    return {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def test_legacy_analysis_history_migration_is_idempotent_and_lossless(legacy_data_dir: Path) -> None:
    original_ids = {"segment-reviewed", "segment-rejected"}

    database.initialize()
    with sqlite3.connect(database.settings.db_path) as con:
        con.row_factory = sqlite3.Row
        first_counts = _table_counts(con)
        assert {row["id"] for row in con.execute("SELECT id FROM segments")} == original_ids
        assert {row["name"] for row in con.execute("PRAGMA table_info(segments)")} >= {
            "analysis_run_id",
            "revision_number",
            "lifecycle_state",
            "retired_at",
        }

        run = con.execute("SELECT * FROM analysis_runs WHERE video_id='video-legacy'").fetchone()
        assert run is not None
        assert run["state"] == "completed"
        assert run["is_current"] == 1
        assert run["analysis_mode"] == "extended"
        assert run["candidate_count"] == 2
        assert run["elapsed_seconds"] == pytest.approx(81.5)

        segments = con.execute(
            """SELECT id, analysis_run_id, revision_number, lifecycle_state, retired_at
               FROM segments ORDER BY id"""
        ).fetchall()
        assert {row["analysis_run_id"] for row in segments} == {run["id"]}
        assert {row["revision_number"] for row in segments} == {1}
        assert {row["lifecycle_state"] for row in segments} == {"current"}
        assert all(row["retired_at"] is None for row in segments)

        revisions = con.execute(
            """SELECT segment_id, analysis_run_id, revision_number, is_current, payload_json
               FROM segment_revisions ORDER BY segment_id"""
        ).fetchall()
        assert {row["segment_id"] for row in revisions} == original_ids
        assert all(row["analysis_run_id"] == run["id"] for row in revisions)
        assert all(row["revision_number"] == 1 and row["is_current"] == 1 for row in revisions)
        payloads = {row["segment_id"]: json.loads(row["payload_json"]) for row in revisions}
        assert payloads["segment-reviewed"]["transcript"].startswith("To jest zachowany")
        assert "rating" not in payloads["segment-reviewed"]
        assert "review_reason" not in payloads["segment-rejected"]

        approved = con.execute(
            "SELECT * FROM segment_reviews WHERE segment_id='segment-reviewed'"
        ).fetchone()
        rejected = con.execute(
            "SELECT * FROM segment_reviews WHERE segment_id='segment-rejected'"
        ).fetchone()
        assert approved["rating"] == "accepted"
        assert approved["censor_profanity"] == 1
        assert approved["remove_pauses"] == 1
        assert approved["archive_audio_path"] == "review-audio/segment-reviewed.m4a"
        assert approved["archive_audio_track"] == 2
        assert approved["reviewed_revision_id"] is not None
        assert rejected["rating"] == "rejected"
        assert rejected["review_reason"] == "czytanie notatki z gry"

        example = con.execute(
            "SELECT * FROM collection_examples WHERE collection_id='collection-best'"
        ).fetchone()
        assert example["segment_id"] == "segment-reviewed"
        assert example["revision_number"] == 1
        assert example["snapshot_video_id"] == "video-legacy"
        assert example["snapshot_start_seconds"] == pytest.approx(10.0)
        assert example["snapshot_end_seconds"] == pytest.approx(26.5)
        assert example["snapshot_transcript"].startswith("To jest zachowany")
        assert example["snapshot_embedding"] == "[0.1,0.2,0.3]"

        assert {
            (row["tag"], row["verdict"])
            for row in con.execute("SELECT tag, verdict FROM tag_feedback")
        } == {("humor", "correct"), ("czytanie", "incorrect")}
        assert con.execute(
            "SELECT reviewed_revision_number FROM preference_feedback WHERE id='feedback-1'"
        ).fetchone()[0] is None
        assert {
            (row["canonical_tag"], row["verdict"], row["reviewed_revision_id"])
            for row in con.execute(
                "SELECT canonical_tag, verdict, reviewed_revision_id FROM segment_tag_reviews"
            )
        } == {
            ("humor", "correct", "legacy-revision:segment-reviewed"),
            ("format: czytanie", "incorrect", "legacy-revision:segment-reviewed"),
        }
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("PRAGMA user_version").fetchone()[0] == database.DATABASE_SCHEMA_VERSION

    database.initialize()
    with sqlite3.connect(database.settings.db_path) as con:
        con.row_factory = sqlite3.Row
        assert _table_counts(con) == first_counts
        assert con.execute(
            "SELECT COUNT(*) FROM maintenance_tasks WHERE name=?",
            (database.ANALYSIS_HISTORY_MIGRATION,),
        ).fetchone()[0] == 1
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_completed_analysis_history_marker_skips_full_backfill_scan(
    legacy_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database.initialize()
    statements: list[str] = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        con = original_connect(*args, **kwargs)
        con.set_trace_callback(statements.append)
        return con

    monkeypatch.setattr(database.sqlite3, "connect", traced_connect)
    database.initialize()

    normalized = [" ".join(statement.upper().split()) for statement in statements]
    assert any(
        "SELECT 1 FROM MAINTENANCE_TASKS WHERE NAME=" in statement
        for statement in normalized
    )
    assert not any("SELECT V.* FROM VIDEOS V" in statement for statement in normalized)
    assert not any(
        "SELECT * FROM SEGMENTS ORDER BY CREATED_AT, ID" in statement
        for statement in normalized
    )


def test_v1_upgrade_quarantines_only_orphans_from_legacy_video_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "legacy-orphans"
    monkeypatch.setattr(database.settings, "clipfinder_data_dir", data_dir)
    _create_legacy_database(database.settings.db_path)
    timestamp = "2026-08-02T12:00:00+00:00"

    # Old request connections did not enable foreign keys.  Reproduce the
    # former delete_video order: dependent chat/tag rows were left behind even
    # though their recording and segment were removed.
    with sqlite3.connect(database.settings.db_path) as con:
        con.execute("PRAGMA foreign_keys = OFF")
        con.executescript(
            """
            CREATE TABLE chat_settings (
                video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
                source_name TEXT NOT NULL DEFAULT '',
                delay_seconds REAL NOT NULL DEFAULT 6,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE chat_messages (
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                seconds REAL NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL
            );
            """
        )
        con.execute(
            """INSERT INTO videos
               (id, original_name, path, duration_seconds, status, created_at, updated_at)
               VALUES ('video-deleted', 'deleted.mp4', 'incoming/deleted.mp4', 120,
                       'ready', ?, ?)""",
            (timestamp, timestamp),
        )
        con.execute(
            """INSERT INTO segments
               (id, video_id, start_seconds, end_seconds, transcript, embedding,
                rating, created_at)
               VALUES ('segment-deleted', 'video-deleted', 4, 12, 'Historyczny klip',
                       '[0.4,0.6]', 'accepted', ?)""",
            (timestamp,),
        )
        con.execute(
            "INSERT INTO chat_settings VALUES ('video-deleted', 'chat.jsonl', 6, ?)",
            (timestamp,),
        )
        con.execute("CREATE TABLE boss_profiles (id TEXT PRIMARY KEY)")
        con.execute(
            """CREATE TABLE boss_reports (
                   id TEXT PRIMARY KEY,
                   profile_id TEXT NOT NULL REFERENCES boss_profiles(id),
                   video_id TEXT NOT NULL REFERENCES videos(id)
               )"""
        )
        con.execute("INSERT INTO boss_profiles VALUES ('legacy-boss-profile')")
        con.execute(
            "INSERT INTO boss_reports VALUES ('legacy-boss-report', 'legacy-boss-profile', 'video-deleted')"
        )
        con.execute(
            "INSERT INTO chat_settings VALUES ('video-legacy', 'kept-chat.jsonl', 6, ?)",
            (timestamp,),
        )
        con.execute(
            "INSERT INTO chat_messages VALUES ('video-deleted', 8, 'viewer', 'reakcja czatu')"
        )
        con.execute(
            "INSERT INTO chat_messages VALUES ('video-legacy', 18, 'viewer', 'zachowana wiadomość')"
        )
        con.execute(
            "INSERT INTO tag_feedback VALUES ('segment-deleted', 'humor', 'correct', ?)",
            (timestamp,),
        )
        con.execute(
            """INSERT INTO preference_feedback
               (id, segment_id, profile, decision, review_reason, embedding,
                features, created_at, updated_at)
               VALUES ('orphan-training-snapshot', 'segment-deleted', 'general',
                       'accepted', '', '[0.4,0.6]', '{"quality":80}', ?, ?)""",
            (timestamp, timestamp),
        )
        con.execute("DELETE FROM segments WHERE video_id='video-deleted'")
        con.execute("DELETE FROM videos WHERE id='video-deleted'")
        assert con.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE video_id='video-deleted'"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM tag_feedback WHERE segment_id='segment-deleted'"
        ).fetchone()[0] == 1

    database.initialize()

    with sqlite3.connect(database.settings.db_path) as con:
        con.row_factory = sqlite3.Row
        assert con.execute(
            "SELECT COUNT(*) FROM chat_settings WHERE video_id='video-deleted'"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE video_id='video-deleted'"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM tag_feedback WHERE segment_id='segment-deleted'"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM chat_settings WHERE video_id='video-legacy'"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE video_id='video-legacy'"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM tag_feedback WHERE segment_id='segment-reviewed'"
        ).fetchone()[0] == 2
        snapshot = con.execute(
            "SELECT decision, embedding, features FROM preference_feedback WHERE id='orphan-training-snapshot'"
        ).fetchone()
        assert dict(snapshot) == {
            "decision": "accepted",
            "embedding": "[0.4,0.6]",
            "features": '{"quality":80}',
        }
        archived = con.execute(
            "SELECT source_table, payload_json FROM legacy_orphan_archive ORDER BY source_table"
        ).fetchall()
        assert [row["source_table"] for row in archived] == [
            "boss_reports", "chat_messages", "chat_settings", "tag_feedback",
        ]
        assert {json.loads(row["payload_json"])["video_id"] for row in archived[:3]} == {
            "video-deleted"
        }
        assert json.loads(archived[3]["payload_json"])["segment_id"] == "segment-deleted"
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_newer_database_schema_is_refused_without_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "newer-data"
    monkeypatch.setattr(database.settings, "clipfinder_data_dir", data_dir)
    data_dir.mkdir(parents=True)
    with sqlite3.connect(database.settings.db_path) as con:
        con.execute("PRAGMA user_version = 999")

    with pytest.raises(RuntimeError, match="newer application version"):
        database.initialize()

    with sqlite3.connect(database.settings.db_path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 999
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0] == 0


def test_foreign_keys_protect_reviewed_history_after_migration(legacy_data_dir: Path) -> None:
    database.initialize()

    with database.connection() as con:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("DELETE FROM segments WHERE id='segment-reviewed'")

    with sqlite3.connect(database.settings.db_path) as con:
        assert con.execute(
            "SELECT rating FROM segment_reviews WHERE segment_id='segment-reviewed'"
        ).fetchone()[0] == "accepted"
        assert con.execute(
            "SELECT COUNT(*) FROM tag_feedback WHERE segment_id='segment-reviewed'"
        ).fetchone()[0] == 2
        assert con.execute(
            "SELECT COUNT(*) FROM segment_tag_reviews WHERE segment_id='segment-reviewed'"
        ).fetchone()[0] == 2
        assert con.execute(
            "SELECT COUNT(*) FROM collection_examples WHERE segment_id='segment-reviewed'"
        ).fetchone()[0] == 1
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
