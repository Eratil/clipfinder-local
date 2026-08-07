import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from app.config import settings


ANALYSIS_HISTORY_MIGRATION = "analysis-history-v1"
TAG_REVIEW_HISTORY_MIGRATION = "tag-review-history-v1"
PREFERENCE_REVISION_SAFETY_MIGRATION = "preference-revision-safety-v1"
# ``PRAGMA user_version`` protects the local library from accidental downgrade
# writes.  Bump this number only when a release changes the SQLite schema, and
# keep every older-to-current migration in ``initialize`` idempotent.
DATABASE_SCHEMA_VERSION = 1

# The immutable revision payload intentionally excludes human decisions.  A
# reanalysis may replace every value below, while ``segment_reviews`` remains
# attached to the stable ``segments.id`` moment.
SEGMENT_MACHINE_COLUMNS = (
    "start_seconds", "end_seconds", "transcript", "keywords", "tags",
    "word_timestamps", "embedding", "quality_score", "quality_signals",
    "short_potential_score", "short_potential_signals", "reading_likelihood",
    "text_reading_likelihood", "visual_reading_likelihood",
    "extended_reading_likelihood", "extended_hook_score",
    "extended_ending_score", "extended_story_signals",
    "boundary_signals", "context_signals",
    "audio_event_score", "game_reaction_score", "voice_expression_score",
    "moment_reaction_score", "moment_reaction_stage", "vision_score",
    "chat_reaction_score", "chat_joy_score", "chat_message_count",
    "chat_unique_authors", "chat_surge", "chat_messages", "duplicate_group",
    "logical_sense_score", "context_score", "self_contained_score",
    "extended_completeness_score", "chat_question_match_score",
    "chat_question_text", "context_before", "context_after",
)


def now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(settings.db_path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 30000")
        con.execute("PRAGMA journal_mode = WAL")
        # Foreign-key enforcement is a per-connection SQLite setting.  Keeping
        # it enabled here makes the declared cascade rules work for normal app
        # requests as well as during a recording reanalysis.
        con.execute("PRAGMA foreign_keys = ON")
        yield con
        con.commit()
    finally:
        con.close()


def initialize() -> None:
    settings.ensure_directories()
    with connection() as con:
        existing_schema_version = int(con.execute("PRAGMA user_version").fetchone()[0])
        if existing_schema_version > DATABASE_SCHEMA_VERSION:
            raise RuntimeError(
                "This ClipFinder data library was opened by a newer application version "
                f"(database schema {existing_schema_version}, supported {DATABASE_SCHEMA_VERSION}). "
                "Install the newer ClipFinder version instead of downgrading."
            )
        con.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                path TEXT NOT NULL,
                source_url TEXT,
                source_removed INTEGER NOT NULL DEFAULT 0,
                source_removed_at TEXT,
                source_size_bytes INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL,
                analysis_seconds REAL NOT NULL DEFAULT 0,
                analysis_mode TEXT NOT NULL DEFAULT 'default',
                status TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                kind TEXT NOT NULL DEFAULT 'analysis',
                payload_json TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                available_at TEXT NOT NULL DEFAULT '',
                lease_owner TEXT,
                lease_expires_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                pause_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analysis_runs (
                id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE RESTRICT,
                parent_run_id TEXT REFERENCES analysis_runs(id) ON DELETE SET NULL,
                sequence INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('running', 'completed', 'failed', 'interrupted')),
                is_current INTEGER NOT NULL DEFAULT 0 CHECK(is_current IN (0, 1)),
                analysis_mode TEXT NOT NULL DEFAULT 'default',
                pipeline_version TEXT NOT NULL DEFAULT 'legacy',
                scoring_version TEXT NOT NULL DEFAULT 'legacy',
                tagging_version TEXT NOT NULL DEFAULT 'legacy',
                matcher_version TEXT NOT NULL DEFAULT 'legacy',
                whisper_model TEXT NOT NULL DEFAULT '',
                whisper_device TEXT NOT NULL DEFAULT '',
                whisper_compute_type TEXT NOT NULL DEFAULT '',
                transcript_audio_track INTEGER NOT NULL DEFAULT 1,
                audio_analysis_mode TEXT NOT NULL DEFAULT 'single',
                candidate_count INTEGER NOT NULL DEFAULT 0,
                elapsed_seconds REAL NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(video_id, sequence)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_runs_current_video
                ON analysis_runs(video_id) WHERE is_current=1;
            CREATE INDEX IF NOT EXISTS idx_analysis_runs_video_sequence
                ON analysis_runs(video_id, sequence DESC);
            CREATE TABLE IF NOT EXISTS segments (
                id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                transcript TEXT NOT NULL DEFAULT '',
                keywords TEXT NOT NULL DEFAULT '[]',
                embedding TEXT,
                rating TEXT NOT NULL DEFAULT 'unrated',
                review_reason TEXT NOT NULL DEFAULT '',
                quality_score INTEGER NOT NULL DEFAULT 0,
                quality_signals TEXT NOT NULL DEFAULT '[]',
                short_potential_score INTEGER NOT NULL DEFAULT -1,
                short_potential_signals TEXT NOT NULL DEFAULT '[]',
                reading_likelihood REAL NOT NULL DEFAULT 0,
                text_reading_likelihood REAL NOT NULL DEFAULT 0,
                visual_reading_likelihood REAL NOT NULL DEFAULT 0,
                extended_reading_likelihood REAL NOT NULL DEFAULT 0,
                extended_hook_score INTEGER NOT NULL DEFAULT -1,
                extended_ending_score INTEGER NOT NULL DEFAULT -1,
                extended_story_signals TEXT NOT NULL DEFAULT '[]',
                boundary_signals TEXT NOT NULL DEFAULT '[]',
                context_signals TEXT NOT NULL DEFAULT '[]',
                audio_event_score INTEGER NOT NULL DEFAULT 0,
                game_reaction_score INTEGER NOT NULL DEFAULT 0,
                voice_expression_score INTEGER NOT NULL DEFAULT 0,
                moment_reaction_score INTEGER NOT NULL DEFAULT 0,
                moment_reaction_stage TEXT NOT NULL DEFAULT '',
                vision_score INTEGER NOT NULL DEFAULT 0,
                chat_reaction_score INTEGER NOT NULL DEFAULT 0,
                chat_joy_score INTEGER NOT NULL DEFAULT 0,
                chat_message_count INTEGER NOT NULL DEFAULT 0,
                chat_unique_authors INTEGER NOT NULL DEFAULT 0,
                chat_surge REAL NOT NULL DEFAULT 0,
                chat_messages TEXT NOT NULL DEFAULT '[]',
                duplicate_group TEXT NOT NULL DEFAULT '',
                logical_sense_score INTEGER NOT NULL DEFAULT -1,
                context_score INTEGER NOT NULL DEFAULT -1,
                self_contained_score INTEGER NOT NULL DEFAULT -1,
                extended_completeness_score INTEGER NOT NULL DEFAULT -1,
                chat_question_match_score INTEGER NOT NULL DEFAULT 0,
                chat_question_text TEXT NOT NULL DEFAULT '',
                context_before TEXT NOT NULL DEFAULT '',
                context_after TEXT NOT NULL DEFAULT '',
                censor_profanity INTEGER NOT NULL DEFAULT 0,
                remove_pauses INTEGER NOT NULL DEFAULT 0,
                archive_audio_path TEXT NOT NULL DEFAULT '',
                archive_audio_track INTEGER NOT NULL DEFAULT 1,
                analysis_run_id TEXT NOT NULL DEFAULT '',
                revision_number INTEGER NOT NULL DEFAULT 1,
                lifecycle_state TEXT NOT NULL DEFAULT 'current',
                retired_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_segments_video_time ON segments(video_id, start_seconds);
            CREATE TABLE IF NOT EXISTS segment_revisions (
                id TEXT PRIMARY KEY,
                segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE RESTRICT,
                analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE RESTRICT,
                revision_number INTEGER NOT NULL CHECK(revision_number >= 1),
                revision_kind TEXT NOT NULL DEFAULT 'analysis',
                is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0, 1)),
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                transcript TEXT NOT NULL DEFAULT '',
                embedding TEXT,
                payload_json TEXT NOT NULL,
                match_confidence REAL,
                match_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(segment_id, revision_number)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_segment_revisions_current
                ON segment_revisions(segment_id) WHERE is_current=1;
            CREATE INDEX IF NOT EXISTS idx_segment_revisions_run_time
                ON segment_revisions(analysis_run_id, start_seconds);
            CREATE TABLE IF NOT EXISTS segment_reviews (
                segment_id TEXT PRIMARY KEY REFERENCES segments(id) ON DELETE RESTRICT,
                reviewed_revision_id TEXT REFERENCES segment_revisions(id) ON DELETE SET NULL,
                rating TEXT NOT NULL DEFAULT 'unrated' CHECK(rating IN ('unrated', 'accepted', 'rejected')),
                review_reason TEXT NOT NULL DEFAULT '',
                censor_profanity INTEGER NOT NULL DEFAULT 0 CHECK(censor_profanity IN (0, 1)),
                remove_pauses INTEGER NOT NULL DEFAULT 0 CHECK(remove_pauses IN (0, 1)),
                archive_audio_path TEXT NOT NULL DEFAULT '',
                archive_audio_track INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_segment_reviews_rating_updated
                ON segment_reviews(rating, updated_at DESC);
            CREATE TABLE IF NOT EXISTS chat_settings (
                video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
                source_name TEXT NOT NULL DEFAULT '',
                delay_seconds REAL NOT NULL DEFAULT 6,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                seconds REAL NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_messages_video_time ON chat_messages(video_id, seconds);
            CREATE TABLE IF NOT EXISTS collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collection_examples (
                collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                revision_number INTEGER,
                snapshot_video_id TEXT NOT NULL DEFAULT '',
                snapshot_start_seconds REAL,
                snapshot_end_seconds REAL,
                snapshot_transcript TEXT NOT NULL DEFAULT '',
                snapshot_embedding TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(collection_id, segment_id)
            );
            CREATE TABLE IF NOT EXISTS external_examples (
                id TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                source_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                transcript TEXT NOT NULL DEFAULT '',
                embedding TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(collection_id, source_path)
            );
            CREATE TABLE IF NOT EXISTS reference_imports (
                id TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                folder_path TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'folder',
                include_subfolders INTEGER NOT NULL DEFAULT 1,
                payload_json TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                total_files INTEGER NOT NULL DEFAULT 0,
                imported_files INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                available_at TEXT NOT NULL DEFAULT '',
                lease_owner TEXT,
                lease_expires_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reference_sources (
                id TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                folder_path TEXT NOT NULL,
                include_subfolders INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(collection_id, folder_path)
            );
            CREATE TABLE IF NOT EXISTS reference_url_sources (
                id TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                source_url TEXT NOT NULL,
                source_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(collection_id, source_url)
            );
            CREATE TABLE IF NOT EXISTS saved_prompts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                prompt TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rejection_reasons (
                reason TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS caption_defaults (
                id INTEGER PRIMARY KEY CHECK(id=1),
                captions_preset TEXT NOT NULL DEFAULT 'highlight',
                base_color TEXT NOT NULL DEFAULT '#FFFFFF',
                active_color TEXT NOT NULL DEFAULT '#FFFF00',
                font_family TEXT NOT NULL DEFAULT 'Inter',
                outline_enabled INTEGER NOT NULL DEFAULT 1,
                outline_color TEXT NOT NULL DEFAULT '#000000',
                glow_enabled INTEGER NOT NULL DEFAULT 0,
                opacity INTEGER NOT NULL DEFAULT 100,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS caption_favorites (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                captions_preset TEXT NOT NULL,
                base_color TEXT NOT NULL,
                active_color TEXT NOT NULL,
                font_family TEXT NOT NULL DEFAULT 'Inter',
                outline_enabled INTEGER NOT NULL DEFAULT 1,
                outline_color TEXT NOT NULL DEFAULT '#000000',
                glow_enabled INTEGER NOT NULL DEFAULT 0,
                opacity INTEGER NOT NULL DEFAULT 100,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS export_defaults (
                id INTEGER PRIMARY KEY CHECK(id=1),
                layout TEXT NOT NULL DEFAULT 'original',
                audio_track INTEGER NOT NULL DEFAULT 1,
                camera_x REAL NOT NULL DEFAULT 0.78,
                camera_y REAL NOT NULL DEFAULT 0.03,
                camera_width REAL NOT NULL DEFAULT 0.11,
                camera_height REAL NOT NULL DEFAULT 0.11,
                game_x REAL NOT NULL DEFAULT 0.22,
                game_y REAL NOT NULL DEFAULT 0.0,
                game_width REAL NOT NULL DEFAULT 0.56,
                game_height REAL NOT NULL DEFAULT 1.0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS layout_presets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                layout TEXT NOT NULL,
                camera_x REAL NOT NULL,
                camera_y REAL NOT NULL,
                camera_width REAL NOT NULL,
                camera_height REAL NOT NULL,
                game_x REAL NOT NULL,
                game_y REAL NOT NULL,
                game_width REAL NOT NULL,
                game_height REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analysis_audio_defaults (
                id INTEGER PRIMARY KEY CHECK(id=1),
                mode TEXT NOT NULL DEFAULT 'split',
                single_track INTEGER NOT NULL DEFAULT 1,
                microphone_track INTEGER NOT NULL DEFAULT 2,
                all_sounds_track INTEGER NOT NULL DEFAULT 1,
                game_track INTEGER NOT NULL DEFAULT 3,
                use_all_sounds INTEGER NOT NULL DEFAULT 1,
                use_game INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_defaults (
                id INTEGER PRIMARY KEY CHECK(id=1),
                active_profile TEXT NOT NULL DEFAULT 'general',
                reference_collection_id TEXT NOT NULL DEFAULT '',
                pattern_set_id TEXT NOT NULL DEFAULT '',
                profanity_filter TEXT NOT NULL DEFAULT 'allow',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_pattern_sets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                profile TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(profile, name)
            );
            CREATE TABLE IF NOT EXISTS discovery_pattern_examples (
                id TEXT PRIMARY KEY,
                pattern_set_id TEXT NOT NULL REFERENCES discovery_pattern_sets(id) ON DELETE CASCADE,
                duration_seconds REAL NOT NULL,
                tags TEXT NOT NULL,
                quality_score INTEGER NOT NULL,
                logical_sense_score INTEGER NOT NULL,
                reading_likelihood REAL NOT NULL,
                embedding TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_pattern_examples_set ON discovery_pattern_examples(pattern_set_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS preference_feedback (
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
            CREATE INDEX IF NOT EXISTS idx_preference_feedback_profile ON preference_feedback(profile, decision, updated_at DESC);
            CREATE TABLE IF NOT EXISTS tag_feedback (
                segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                verdict TEXT NOT NULL CHECK(verdict IN ('correct', 'incorrect')),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(segment_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_tag_feedback_verdict ON tag_feedback(verdict, updated_at DESC);
            CREATE TABLE IF NOT EXISTS segment_tag_reviews (
                id TEXT PRIMARY KEY,
                segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE RESTRICT,
                reviewed_revision_id TEXT NOT NULL REFERENCES segment_revisions(id) ON DELETE RESTRICT,
                canonical_tag TEXT NOT NULL,
                verdict TEXT NOT NULL CHECK(verdict IN ('correct', 'incorrect')),
                tagging_version TEXT NOT NULL DEFAULT 'legacy',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(segment_id, reviewed_revision_id, canonical_tag)
            );
            CREATE INDEX IF NOT EXISTS idx_segment_tag_reviews_revision
                ON segment_tag_reviews(segment_id, reviewed_revision_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_segment_tag_reviews_verdict
                ON segment_tag_reviews(verdict, updated_at DESC);
            CREATE TABLE IF NOT EXISTS maintenance_tasks (
                name TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL
            );
            """
        )
        job_columns = {item["name"] for item in con.execute("PRAGMA table_info(jobs)").fetchall()}
        for name, declaration in {
            "kind": "TEXT NOT NULL DEFAULT 'analysis'",
            "payload_json": "TEXT NOT NULL DEFAULT '{}'",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 3",
            "available_at": "TEXT NOT NULL DEFAULT ''",
            "lease_owner": "TEXT",
            "lease_expires_at": "TEXT",
            "last_error": "TEXT NOT NULL DEFAULT ''",
            "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
            "pause_requested": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in job_columns:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
        con.execute("UPDATE jobs SET available_at=created_at WHERE available_at='' OR available_at IS NULL")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(state, available_at, created_at)")

        reference_import_columns = {
            item["name"] for item in con.execute("PRAGMA table_info(reference_imports)").fetchall()
        }
        for name, declaration in {
            "kind": "TEXT NOT NULL DEFAULT 'folder'",
            "include_subfolders": "INTEGER NOT NULL DEFAULT 1",
            "payload_json": "TEXT NOT NULL DEFAULT '{}'",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 3",
            "available_at": "TEXT NOT NULL DEFAULT ''",
            "lease_owner": "TEXT",
            "lease_expires_at": "TEXT",
            "last_error": "TEXT NOT NULL DEFAULT ''",
            "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in reference_import_columns:
                con.execute(f"ALTER TABLE reference_imports ADD COLUMN {name} {declaration}")
        con.execute(
            "UPDATE reference_imports SET available_at=created_at WHERE available_at='' OR available_at IS NULL"
        )
        con.execute(
            """UPDATE reference_imports SET kind='url', include_subfolders=0
               WHERE folder_path LIKE 'http://%' OR folder_path LIKE 'https://%'"""
        )
        con.execute(
            """UPDATE reference_imports
               SET include_subfolders=COALESCE((
                   SELECT rs.include_subfolders FROM reference_sources rs
                   WHERE rs.collection_id=reference_imports.collection_id
                     AND rs.folder_path=reference_imports.folder_path
                   LIMIT 1
               ), include_subfolders)
               WHERE kind='folder'"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_reference_imports_claim "
            "ON reference_imports(state, available_at, created_at)"
        )

        columns = {item["name"] for item in con.execute("PRAGMA table_info(segments)").fetchall()}
        if "tags" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        if "word_timestamps" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN word_timestamps TEXT NOT NULL DEFAULT '[]'")
        if "censor_profanity" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN censor_profanity INTEGER NOT NULL DEFAULT 0")
        if "remove_pauses" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN remove_pauses INTEGER NOT NULL DEFAULT 0")
        if "archive_audio_path" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN archive_audio_path TEXT NOT NULL DEFAULT ''")
        if "archive_audio_track" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN archive_audio_track INTEGER NOT NULL DEFAULT 1")
        if "analysis_run_id" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN analysis_run_id TEXT NOT NULL DEFAULT ''")
        if "revision_number" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN revision_number INTEGER NOT NULL DEFAULT 1")
        if "lifecycle_state" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'current'")
        if "retired_at" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN retired_at TEXT")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_segments_video_lifecycle_time ON segments(video_id, lifecycle_state, start_seconds)"
        )
        if "review_reason" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN review_reason TEXT NOT NULL DEFAULT ''")
        if "quality_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN quality_score INTEGER NOT NULL DEFAULT 0")
        if "quality_signals" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN quality_signals TEXT NOT NULL DEFAULT '[]'")
        if "short_potential_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN short_potential_score INTEGER NOT NULL DEFAULT -1")
        if "short_potential_signals" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN short_potential_signals TEXT NOT NULL DEFAULT '[]'")
        if "reading_likelihood" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN reading_likelihood REAL NOT NULL DEFAULT 0")
        if "text_reading_likelihood" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN text_reading_likelihood REAL NOT NULL DEFAULT 0")
        if "visual_reading_likelihood" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN visual_reading_likelihood REAL NOT NULL DEFAULT 0")
            # Before provenance fields existed the combined probability could
            # include a text-heavy frame. Keep it as conservative visual
            # evidence; the text and Extended components are recomputed.
            con.execute(
                "UPDATE segments SET visual_reading_likelihood=reading_likelihood WHERE reading_likelihood > 0"
            )
        if "extended_reading_likelihood" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN extended_reading_likelihood REAL NOT NULL DEFAULT 0")
        if "extended_hook_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN extended_hook_score INTEGER NOT NULL DEFAULT -1")
        if "extended_ending_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN extended_ending_score INTEGER NOT NULL DEFAULT -1")
        if "extended_story_signals" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN extended_story_signals TEXT NOT NULL DEFAULT '[]'")
        if "boundary_signals" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN boundary_signals TEXT NOT NULL DEFAULT '[]'")
        if "context_signals" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN context_signals TEXT NOT NULL DEFAULT '[]'")
        if "audio_event_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN audio_event_score INTEGER NOT NULL DEFAULT 0")
        if "game_reaction_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN game_reaction_score INTEGER NOT NULL DEFAULT 0")
        if "voice_expression_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN voice_expression_score INTEGER NOT NULL DEFAULT 0")
        if "moment_reaction_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN moment_reaction_score INTEGER NOT NULL DEFAULT 0")
        if "moment_reaction_stage" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN moment_reaction_stage TEXT NOT NULL DEFAULT ''")
        if "vision_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN vision_score INTEGER NOT NULL DEFAULT 0")
        if "chat_reaction_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN chat_reaction_score INTEGER NOT NULL DEFAULT 0")
        if "chat_joy_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN chat_joy_score INTEGER NOT NULL DEFAULT 0")
        if "chat_message_count" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN chat_message_count INTEGER NOT NULL DEFAULT 0")
        if "chat_unique_authors" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN chat_unique_authors INTEGER NOT NULL DEFAULT 0")
        if "chat_surge" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN chat_surge REAL NOT NULL DEFAULT 0")
        if "chat_messages" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN chat_messages TEXT NOT NULL DEFAULT '[]'")
        if "duplicate_group" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN duplicate_group TEXT NOT NULL DEFAULT ''")
        if "logical_sense_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN logical_sense_score INTEGER NOT NULL DEFAULT -1")
        if "context_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN context_score INTEGER NOT NULL DEFAULT -1")
        if "self_contained_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN self_contained_score INTEGER NOT NULL DEFAULT -1")
        if "extended_completeness_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN extended_completeness_score INTEGER NOT NULL DEFAULT -1")
        if "chat_question_match_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN chat_question_match_score INTEGER NOT NULL DEFAULT 0")
        if "chat_question_text" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN chat_question_text TEXT NOT NULL DEFAULT ''")
        if "context_before" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN context_before TEXT NOT NULL DEFAULT ''")
        if "context_after" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN context_after TEXT NOT NULL DEFAULT ''")
        video_columns = {item["name"] for item in con.execute("PRAGMA table_info(videos)").fetchall()}
        if "source_url" not in video_columns:
            con.execute("ALTER TABLE videos ADD COLUMN source_url TEXT")
        if "transcript_audio_track" not in video_columns:
            con.execute("ALTER TABLE videos ADD COLUMN transcript_audio_track INTEGER NOT NULL DEFAULT 1")
        if "audio_analysis_mode" not in video_columns:
            con.execute("ALTER TABLE videos ADD COLUMN audio_analysis_mode TEXT NOT NULL DEFAULT 'single'")
        if "analysis_seconds" not in video_columns:
            con.execute("ALTER TABLE videos ADD COLUMN analysis_seconds REAL NOT NULL DEFAULT 0")
        if "analysis_mode" not in video_columns:
            con.execute("ALTER TABLE videos ADD COLUMN analysis_mode TEXT NOT NULL DEFAULT 'default'")
        if "source_removed" not in video_columns:
            con.execute("ALTER TABLE videos ADD COLUMN source_removed INTEGER NOT NULL DEFAULT 0")
        if "source_removed_at" not in video_columns:
            con.execute("ALTER TABLE videos ADD COLUMN source_removed_at TEXT")
        if "source_size_bytes" not in video_columns:
            con.execute("ALTER TABLE videos ADD COLUMN source_size_bytes INTEGER NOT NULL DEFAULT 0")
        con.execute(
            """UPDATE jobs SET kind='remote_import'
               WHERE kind='analysis' AND state IN ('queued', 'running', 'interrupted')
                 AND video_id IN (
                     SELECT id FROM videos
                     WHERE source_url IS NOT NULL AND source_url != '' AND path LIKE '%.download'
                 )"""
        )
        discovery_columns = {item["name"] for item in con.execute("PRAGMA table_info(discovery_defaults)").fetchall()}
        if "reference_collection_id" not in discovery_columns:
            con.execute("ALTER TABLE discovery_defaults ADD COLUMN reference_collection_id TEXT NOT NULL DEFAULT ''")
        if "pattern_set_id" not in discovery_columns:
            con.execute("ALTER TABLE discovery_defaults ADD COLUMN pattern_set_id TEXT NOT NULL DEFAULT ''")
        if "profanity_filter" not in discovery_columns:
            con.execute("ALTER TABLE discovery_defaults ADD COLUMN profanity_filter TEXT NOT NULL DEFAULT 'allow'")
        collection_example_columns = {item["name"] for item in con.execute("PRAGMA table_info(collection_examples)").fetchall()}
        for name, declaration in {
            "revision_number": "INTEGER",
            "snapshot_video_id": "TEXT NOT NULL DEFAULT ''",
            "snapshot_start_seconds": "REAL",
            "snapshot_end_seconds": "REAL",
            "snapshot_transcript": "TEXT NOT NULL DEFAULT ''",
            "snapshot_embedding": "TEXT",
        }.items():
            if name not in collection_example_columns:
                con.execute(f"ALTER TABLE collection_examples ADD COLUMN {name} {declaration}")
        preference_columns = {item["name"] for item in con.execute("PRAGMA table_info(preference_feedback)").fetchall()}
        if "reviewed_revision_number" not in preference_columns:
            con.execute("ALTER TABLE preference_feedback ADD COLUMN reviewed_revision_number INTEGER")
        for table in ("caption_defaults", "caption_favorites"):
            caption_columns = {item["name"] for item in con.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, declaration in {
                "font_family": "TEXT NOT NULL DEFAULT 'Inter'",
                "outline_enabled": "INTEGER NOT NULL DEFAULT 1",
                "outline_color": "TEXT NOT NULL DEFAULT '#000000'",
                "glow_enabled": "INTEGER NOT NULL DEFAULT 0",
                "opacity": "INTEGER NOT NULL DEFAULT 100",
            }.items():
                if name not in caption_columns:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        export_columns = {item["name"] for item in con.execute("PRAGMA table_info(export_defaults)").fetchall()}
        for name, default in {
            "camera_x": "0.78", "camera_y": "0.03", "camera_width": "0.11", "camera_height": "0.11",
            "game_x": "0.22", "game_y": "0.0", "game_width": "0.56", "game_height": "1.0",
        }.items():
            if name not in export_columns:
                con.execute(f"ALTER TABLE export_defaults ADD COLUMN {name} REAL NOT NULL DEFAULT {default}")
        con.execute(
            "INSERT OR IGNORE INTO caption_defaults (id, captions_preset, base_color, active_color, font_family, outline_enabled, outline_color, glow_enabled, opacity, updated_at) VALUES (1, 'highlight', '#FFFFFF', '#FFFF00', 'Inter', 1, '#000000', 0, 100, ?)",
            (now(),),
        )
        con.execute(
            "INSERT OR IGNORE INTO export_defaults (id, layout, audio_track, updated_at) VALUES (1, 'original', 1, ?)",
            (now(),),
        )
        con.execute(
            "INSERT OR IGNORE INTO analysis_audio_defaults (id, mode, single_track, microphone_track, all_sounds_track, game_track, use_all_sounds, use_game, updated_at) VALUES (1, 'split', 1, 2, 1, 3, 1, 1, ?)",
            (now(),),
        )
        con.execute(
            "INSERT OR IGNORE INTO discovery_defaults (id, active_profile, updated_at) VALUES (1, 'general', ?)",
            (now(),),
        )
        if existing_schema_version < DATABASE_SCHEMA_VERSION:
            _quarantine_known_legacy_orphans(con)
        _backfill_analysis_history(con)
        _harden_legacy_preference_revisions(con)
        _backfill_tag_review_history(con)
        if existing_schema_version < DATABASE_SCHEMA_VERSION:
            violation = con.execute("PRAGMA foreign_key_check").fetchone()
            if violation:
                raise RuntimeError(
                    "ClipFinder could not safely finish the database upgrade because "
                    f"a foreign-key relationship is invalid: {tuple(violation)}"
                )
            con.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")


def segment_machine_payload(segment: dict) -> dict:
    """Return the immutable, machine-produced portion of a segment row."""
    return {column: segment.get(column) for column in SEGMENT_MACHINE_COLUMNS}


def _quarantine_known_legacy_orphans(con: sqlite3.Connection) -> None:
    """Repair only rows orphaned by the pre-v1 recording deletion flow.

    Old releases enabled foreign keys only on their startup connection.  Their
    recording deletion path explicitly removed jobs, collection examples,
    segments and the video itself, but it relied on inactive cascades for chat
    rows and legacy tag verdicts.  Preserve every such row as JSON in a local
    quarantine before removing it from the constrained table.  The intentionally
    FK-free ``preference_feedback`` training snapshot is never touched.
    """
    con.execute(
        """CREATE TABLE IF NOT EXISTS legacy_orphan_archive (
               id TEXT PRIMARY KEY,
               source_table TEXT NOT NULL,
               source_rowid INTEGER NOT NULL,
               payload_json TEXT NOT NULL,
               archived_at TEXT NOT NULL
           )"""
    )
    timestamp = now()
    known_orphans = [
        (
            "chat_messages",
            "SELECT rowid AS legacy_rowid, * FROM chat_messages "
            "WHERE NOT EXISTS (SELECT 1 FROM videos v WHERE v.id=chat_messages.video_id)",
        ),
        (
            "chat_settings",
            "SELECT rowid AS legacy_rowid, * FROM chat_settings "
            "WHERE NOT EXISTS (SELECT 1 FROM videos v WHERE v.id=chat_settings.video_id)",
        ),
        (
            "tag_feedback",
            "SELECT rowid AS legacy_rowid, * FROM tag_feedback "
            "WHERE NOT EXISTS (SELECT 1 FROM segments s WHERE s.id=tag_feedback.segment_id)",
        ),
    ]
    # Boss-death tracking was removed from ClipFinder, but some pre-v1
    # databases can still contain its tables. A report pointing at a deleted
    # recording would make ``foreign_key_check`` fail and roll back the whole
    # migration forever. Keep the unsupported legacy record in the same
    # quarantine archive as the other known historical leftovers.
    legacy_tables = {
        str(row["name"])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "boss_reports" in legacy_tables:
        if "boss_profiles" in legacy_tables:
            boss_report_query = (
                "SELECT rowid AS legacy_rowid, * FROM boss_reports "
                "WHERE NOT EXISTS (SELECT 1 FROM videos v WHERE v.id=boss_reports.video_id) "
                "OR NOT EXISTS (SELECT 1 FROM boss_profiles p WHERE p.id=boss_reports.profile_id)"
            )
        else:
            # Without the corresponding profile table the report cannot be
            # interpreted by any supported version, so archive it as well.
            boss_report_query = "SELECT rowid AS legacy_rowid, * FROM boss_reports"
        known_orphans.append(("boss_reports", boss_report_query))
    for source_table, query in known_orphans:
        for raw in con.execute(query).fetchall():
            payload = dict(raw)
            source_rowid = int(payload.pop("legacy_rowid"))
            payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            archive_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"clipfinder:legacy-orphan:{source_table}:{source_rowid}:{payload_json}",
            ))
            con.execute(
                """INSERT OR IGNORE INTO legacy_orphan_archive
                   (id, source_table, source_rowid, payload_json, archived_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (archive_id, source_table, source_rowid, payload_json, timestamp),
            )
            con.execute(f"DELETE FROM {source_table} WHERE rowid=?", (source_rowid,))


def _backfill_analysis_history(con: sqlite3.Connection) -> None:
    """Idempotently project legacy rows into the versioned history tables."""
    if con.execute(
        "SELECT 1 FROM maintenance_tasks WHERE name=?",
        (ANALYSIS_HISTORY_MIGRATION,),
    ).fetchone():
        return
    timestamp = now()
    videos = con.execute(
        """SELECT v.* FROM videos v
           WHERE EXISTS (SELECT 1 FROM segments s WHERE s.video_id=v.id)
           ORDER BY v.created_at, v.id"""
    ).fetchall()
    for video_row in videos:
        video = dict(video_row)
        current = con.execute(
            "SELECT id FROM analysis_runs WHERE video_id=? AND is_current=1 ORDER BY sequence DESC LIMIT 1",
            (video["id"],),
        ).fetchone()
        if current:
            run_id = str(current["id"])
        else:
            run_id = f"legacy-run:{video['id']}"
            next_sequence = int(con.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM analysis_runs WHERE video_id=?",
                (video["id"],),
            ).fetchone()["value"])
            con.execute(
                """INSERT OR IGNORE INTO analysis_runs
                   (id, video_id, sequence, state, is_current, analysis_mode, pipeline_version,
                    scoring_version, tagging_version, matcher_version, whisper_model,
                    transcript_audio_track, audio_analysis_mode, candidate_count, elapsed_seconds,
                    started_at, completed_at, created_at)
                   VALUES (?, ?, ?, 'completed', 1, ?, 'legacy', 'legacy', 'legacy', 'legacy', ?, ?, ?,
                           (SELECT COUNT(*) FROM segments WHERE video_id=?), ?, ?, ?, ?)""",
                (
                    run_id, video["id"], next_sequence, video.get("analysis_mode") or "default",
                    "", int(video.get("transcript_audio_track") or 1), video.get("audio_analysis_mode") or "single",
                    video["id"], float(video.get("analysis_seconds") or 0), video.get("created_at") or timestamp,
                    video.get("updated_at") or timestamp, video.get("created_at") or timestamp,
                ),
            )
        con.execute(
            """UPDATE segments SET analysis_run_id=?, revision_number=CASE WHEN revision_number < 1 THEN 1 ELSE revision_number END,
                      lifecycle_state=CASE WHEN lifecycle_state='' THEN 'current' ELSE lifecycle_state END
               WHERE video_id=? AND analysis_run_id=''""",
            (run_id, video["id"]),
        )

    segments = [dict(item) for item in con.execute("SELECT * FROM segments ORDER BY created_at, id").fetchall()]
    for segment in segments:
        run_id = str(segment.get("analysis_run_id") or f"legacy-run:{segment['video_id']}")
        revision_number = max(1, int(segment.get("revision_number") or 1))
        revision_id = f"legacy-revision:{segment['id']}" if revision_number == 1 else f"revision:{segment['id']}:{revision_number}"
        payload = json.dumps(segment_machine_payload(segment), ensure_ascii=False, separators=(",", ":"))
        con.execute(
            """INSERT OR IGNORE INTO segment_revisions
               (id, segment_id, analysis_run_id, revision_number, revision_kind, is_current,
                start_seconds, end_seconds, transcript, embedding, payload_json, created_at)
               VALUES (?, ?, ?, ?, 'legacy', 1, ?, ?, ?, ?, ?, ?)""",
            (
                revision_id, segment["id"], run_id, revision_number,
                segment["start_seconds"], segment["end_seconds"], segment.get("transcript") or "",
                segment.get("embedding"), payload, segment.get("created_at") or timestamp,
            ),
        )
        current_revision = con.execute(
            "SELECT id FROM segment_revisions WHERE segment_id=? AND is_current=1 ORDER BY revision_number DESC LIMIT 1",
            (segment["id"],),
        ).fetchone()
        con.execute(
            """INSERT OR IGNORE INTO segment_reviews
               (segment_id, reviewed_revision_id, rating, review_reason, censor_profanity,
                remove_pauses, archive_audio_path, archive_audio_track, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                segment["id"], current_revision["id"] if current_revision else None,
                segment.get("rating") or "unrated", segment.get("review_reason") or "",
                int(bool(segment.get("censor_profanity"))), int(bool(segment.get("remove_pauses"))),
                segment.get("archive_audio_path") or "", int(segment.get("archive_audio_track") or 1),
                segment.get("created_at") or timestamp, segment.get("created_at") or timestamp,
            ),
        )

    con.execute(
        """UPDATE collection_examples
              SET revision_number=COALESCE(revision_number, (SELECT revision_number FROM segments s WHERE s.id=segment_id)),
                  snapshot_video_id=CASE WHEN snapshot_video_id='' THEN COALESCE((SELECT video_id FROM segments s WHERE s.id=segment_id), '') ELSE snapshot_video_id END,
                  snapshot_start_seconds=COALESCE(snapshot_start_seconds, (SELECT start_seconds FROM segments s WHERE s.id=segment_id)),
                  snapshot_end_seconds=COALESCE(snapshot_end_seconds, (SELECT end_seconds FROM segments s WHERE s.id=segment_id)),
                  snapshot_transcript=CASE WHEN snapshot_transcript='' THEN COALESCE((SELECT transcript FROM segments s WHERE s.id=segment_id), '') ELSE snapshot_transcript END,
                  snapshot_embedding=COALESCE(snapshot_embedding, (SELECT embedding FROM segments s WHERE s.id=segment_id))"""
    )
    con.execute(
        "INSERT OR REPLACE INTO maintenance_tasks (name, completed_at) VALUES (?, ?)",
        (ANALYSIS_HISTORY_MIGRATION, timestamp),
    )


def _harden_legacy_preference_revisions(con: sqlite3.Connection) -> None:
    """Mark pre-versioning training snapshots as unbound, never falsely current.

    Older releases did not record which machine revision was visible when the
    click happened. Guessing the newest revision lets a later chat import pair
    an old decision with new features. The snapshot remains useful for ranking,
    but only a fresh review may bind it to a revision and make it refreshable.
    """
    if con.execute(
        "SELECT 1 FROM maintenance_tasks WHERE name=?",
        (PREFERENCE_REVISION_SAFETY_MIGRATION,),
    ).fetchone():
        return
    con.execute("UPDATE preference_feedback SET reviewed_revision_number=NULL")
    con.execute(
        "INSERT INTO maintenance_tasks (name, completed_at) VALUES (?, ?)",
        (PREFERENCE_REVISION_SAFETY_MIGRATION, now()),
    )


def _backfill_tag_review_history(con: sqlite3.Connection) -> None:
    """Attach legacy tag verdicts to the exact machine revision they reviewed.

    The old ``tag_feedback`` table is retained temporarily as a compatibility
    projection.  New reads use ``segment_tag_reviews`` so a reanalysis cannot
    make a verdict about an old tag set appear as a review of the new one.
    """
    if con.execute(
        "SELECT 1 FROM maintenance_tasks WHERE name=?",
        (TAG_REVIEW_HISTORY_MIGRATION,),
    ).fetchone():
        return

    from app.services.tag_taxonomy import canonical_tag

    timestamp = now()
    legacy_rows = con.execute(
        """SELECT tf.segment_id, tf.tag, tf.verdict, tf.updated_at,
                  sr.id AS revision_id, COALESCE(ar.tagging_version, 'legacy') AS tagging_version
           FROM tag_feedback tf
           JOIN segment_revisions sr ON sr.segment_id=tf.segment_id AND sr.is_current=1
           LEFT JOIN analysis_runs ar ON ar.id=sr.analysis_run_id
           ORDER BY tf.updated_at DESC, tf.segment_id, tf.tag"""
    ).fetchall()
    for item in legacy_rows:
        tag = canonical_tag(item["tag"])
        if not tag:
            continue
        identity = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"clipfinder:tag-review:{item['segment_id']}:{item['revision_id']}:{tag.casefold()}",
        ))
        con.execute(
            """INSERT OR IGNORE INTO segment_tag_reviews
               (id, segment_id, reviewed_revision_id, canonical_tag, verdict,
                tagging_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                identity, item["segment_id"], item["revision_id"], tag,
                item["verdict"], item["tagging_version"] or "legacy",
                item["updated_at"] or timestamp, item["updated_at"] or timestamp,
            ),
        )
    con.execute(
        "INSERT INTO maintenance_tasks (name, completed_at) VALUES (?, ?)",
        (TAG_REVIEW_HISTORY_MIGRATION, timestamp),
    )


def rows(query: str, parameters: tuple = ()) -> list[dict]:
    with connection() as con:
        return [dict(row) for row in con.execute(query, parameters).fetchall()]


def row(query: str, parameters: tuple = ()) -> dict | None:
    result = rows(query, parameters)
    return result[0] if result else None


def maintenance_task_completed(name: str) -> bool:
    """Return whether a potentially expensive one-off data migration has run."""
    return row("SELECT name FROM maintenance_tasks WHERE name=?", (name,)) is not None


def mark_maintenance_task_completed(name: str) -> None:
    """Mark a completed data migration only after its work has succeeded."""
    with connection() as con:
        con.execute(
            "INSERT OR REPLACE INTO maintenance_tasks (name, completed_at) VALUES (?, ?)",
            (name, now()),
        )


def _tag_feedback_by_segment(segment_ids: list[str]) -> dict[str, dict[str, str]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    feedback: dict[str, dict[str, str]] = {segment_id: {} for segment_id in segment_ids}
    try:
        items = rows(
            f"""SELECT tr.segment_id, tr.canonical_tag AS tag, tr.verdict
                FROM segment_tag_reviews tr
                JOIN segments s ON s.id=tr.segment_id
                JOIN segment_revisions sr
                  ON sr.segment_id=s.id AND sr.revision_number=s.revision_number
                 AND sr.id=tr.reviewed_revision_id
                WHERE tr.segment_id IN ({placeholders})
                ORDER BY tr.updated_at""",
            tuple(segment_ids),
        )
    except sqlite3.OperationalError:
        # Used only while a legacy database is being opened before migration.
        items = rows(
            f"SELECT segment_id, tag, verdict FROM tag_feedback WHERE segment_id IN ({placeholders})",
            tuple(segment_ids),
        )
    for item in items:
        feedback.setdefault(item["segment_id"], {})[item["tag"]] = item["verdict"]
    return feedback


def _reviews_by_segment(segment_ids: list[str]) -> dict[str, dict]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    try:
        return {
            item["segment_id"]: item
            for item in rows(
                f"SELECT * FROM segment_reviews WHERE segment_id IN ({placeholders})",
                tuple(segment_ids),
            )
        }
    except sqlite3.OperationalError:
        # Only relevant while opening a pre-migration database during startup.
        return {}


def _current_revisions_by_segment(segment_ids: list[str]) -> dict[str, str | None]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    try:
        return {
            item["segment_id"]: item.get("revision_id")
            for item in rows(
                f"""SELECT s.id AS segment_id, sr.id AS revision_id
                    FROM segments s
                    LEFT JOIN segment_revisions sr
                      ON sr.segment_id=s.id AND sr.revision_number=s.revision_number
                    WHERE s.id IN ({placeholders})""",
                tuple(segment_ids),
            )
        }
    except sqlite3.OperationalError:
        # Only relevant while opening a pre-versioned database during startup.
        return {segment_id: None for segment_id in segment_ids}


def sync_segment_review(con: sqlite3.Connection, segment_id: str) -> None:
    """Dual-write the legacy projection into the canonical review row."""
    segment = con.execute(
        """SELECT id, rating, review_reason, censor_profanity, remove_pauses,
                  archive_audio_path, archive_audio_track, revision_number, created_at
           FROM segments WHERE id=?""",
        (segment_id,),
    ).fetchone()
    if not segment:
        return
    revision = con.execute(
        "SELECT id FROM segment_revisions WHERE segment_id=? AND revision_number=?",
        (segment_id, int(segment["revision_number"] or 1)),
    ).fetchone()
    timestamp = now()
    con.execute(
        """INSERT INTO segment_reviews
           (segment_id, reviewed_revision_id, rating, review_reason, censor_profanity,
            remove_pauses, archive_audio_path, archive_audio_track, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(segment_id) DO UPDATE SET
             reviewed_revision_id=excluded.reviewed_revision_id,
             rating=excluded.rating, review_reason=excluded.review_reason,
             censor_profanity=excluded.censor_profanity, remove_pauses=excluded.remove_pauses,
             archive_audio_path=excluded.archive_audio_path, archive_audio_track=excluded.archive_audio_track,
             updated_at=excluded.updated_at""",
        (
            segment_id, revision["id"] if revision else None, segment["rating"], segment["review_reason"],
            int(bool(segment["censor_profanity"])), int(bool(segment["remove_pauses"])),
            segment["archive_audio_path"], int(segment["archive_audio_track"] or 1),
            segment["created_at"] or timestamp, timestamp,
        ),
    )


_UNLOADED_REVISION = object()


def serialize_segment(
    segment: dict,
    tag_feedback: dict[str, str] | None = None,
    review: dict | None = None,
    current_revision_id: str | None | object = _UNLOADED_REVISION,
) -> dict:
    from app.services.tag_taxonomy import canonical_tag, canonicalize_tags

    if review is None:
        review = _reviews_by_segment([segment["id"]]).get(segment["id"])
    if review:
        for key in (
            "rating", "review_reason", "censor_profanity", "remove_pauses",
            "archive_audio_path", "archive_audio_track",
        ):
            segment[key] = review.get(key, segment.get(key))
        segment["reviewed_revision_id"] = review.get("reviewed_revision_id")
        if current_revision_id is _UNLOADED_REVISION:
            current_revision = row(
                "SELECT id FROM segment_revisions WHERE segment_id=? AND revision_number=?",
                (segment["id"], int(segment.get("revision_number") or 1)),
            ) if segment.get("revision_number") else None
            resolved_revision_id = (current_revision or {}).get("id")
        else:
            resolved_revision_id = current_revision_id
        has_review_data = (
            str(review.get("rating") or "unrated") != "unrated"
            or bool(review.get("censor_profanity"))
            or bool(review.get("remove_pauses"))
            or bool(review.get("archive_audio_path"))
        )
        segment["review_stale"] = bool(
            has_review_data and review.get("reviewed_revision_id")
            and review.get("reviewed_revision_id") != resolved_revision_id
        )
    segment["keywords"] = json.loads(segment["keywords"])
    segment["tags"] = canonicalize_tags(json.loads(segment.get("tags") or "[]"))
    segment["word_timestamps"] = json.loads(segment.get("word_timestamps") or "[]")
    segment["short_potential_signals"] = json.loads(segment.get("short_potential_signals") or "[]")
    segment["quality_signals"] = json.loads(segment.get("quality_signals") or "[]")
    segment["boundary_signals"] = json.loads(segment.get("boundary_signals") or "[]")
    segment["context_signals"] = json.loads(segment.get("context_signals") or "[]")
    segment["extended_story_signals"] = json.loads(segment.get("extended_story_signals") or "[]")
    segment["chat_messages"] = json.loads(segment.get("chat_messages") or "[]")
    raw_feedback = tag_feedback if tag_feedback is not None else _tag_feedback_by_segment([segment["id"]]).get(segment["id"], {})
    segment["tag_feedback"] = {
        canonical: verdict
        for tag, verdict in raw_feedback.items()
        if (canonical := canonical_tag(tag)) is not None
    }
    segment["censor_profanity"] = bool(segment.get("censor_profanity"))
    segment["remove_pauses"] = bool(segment.get("remove_pauses"))
    segment.pop("embedding", None)
    return segment


def serialize_segments(segments: list[dict]) -> list[dict]:
    segment_ids = [segment["id"] for segment in segments]
    feedback = _tag_feedback_by_segment(segment_ids)
    reviews = _reviews_by_segment(segment_ids)
    revisions = _current_revisions_by_segment(segment_ids)
    return [
        serialize_segment(
            segment,
            feedback.get(segment["id"], {}),
            reviews.get(segment["id"]),
            revisions.get(segment["id"]),
        )
        for segment in segments
    ]
