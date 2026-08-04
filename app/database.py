import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from app.config import settings


def now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(settings.db_path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 30000")
        con.execute("PRAGMA journal_mode = WAL")
        yield con
        con.commit()
    finally:
        con.close()


def initialize() -> None:
    settings.ensure_directories()
    with connection() as con:
        con.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                path TEXT NOT NULL,
                source_url TEXT,
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
                state TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
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
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_segments_video_time ON segments(video_id, start_seconds);
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
                state TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                total_files INTEGER NOT NULL DEFAULT 0,
                imported_files INTEGER NOT NULL DEFAULT 0,
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
            CREATE TABLE IF NOT EXISTS maintenance_tasks (
                name TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL
            );
            """
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
        discovery_columns = {item["name"] for item in con.execute("PRAGMA table_info(discovery_defaults)").fetchall()}
        if "reference_collection_id" not in discovery_columns:
            con.execute("ALTER TABLE discovery_defaults ADD COLUMN reference_collection_id TEXT NOT NULL DEFAULT ''")
        if "pattern_set_id" not in discovery_columns:
            con.execute("ALTER TABLE discovery_defaults ADD COLUMN pattern_set_id TEXT NOT NULL DEFAULT ''")
        if "profanity_filter" not in discovery_columns:
            con.execute("ALTER TABLE discovery_defaults ADD COLUMN profanity_filter TEXT NOT NULL DEFAULT 'allow'")
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
        con.execute("UPDATE jobs SET state='interrupted', message='Server was restarted', updated_at=? WHERE state IN ('queued', 'running')", (now(),))
        con.execute("UPDATE videos SET status='interrupted', updated_at=? WHERE status IN ('queued', 'processing')", (now(),))
        con.execute("UPDATE reference_imports SET state='interrupted', message='Server was restarted', updated_at=? WHERE state IN ('queued', 'running')", (now(),))


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
    for item in rows(f"SELECT segment_id, tag, verdict FROM tag_feedback WHERE segment_id IN ({placeholders})", tuple(segment_ids)):
        feedback.setdefault(item["segment_id"], {})[item["tag"]] = item["verdict"]
    return feedback


def serialize_segment(segment: dict, tag_feedback: dict[str, str] | None = None) -> dict:
    segment["keywords"] = json.loads(segment["keywords"])
    segment["tags"] = json.loads(segment.get("tags") or "[]")
    segment["word_timestamps"] = json.loads(segment.get("word_timestamps") or "[]")
    segment["short_potential_signals"] = json.loads(segment.get("short_potential_signals") or "[]")
    segment["quality_signals"] = json.loads(segment.get("quality_signals") or "[]")
    segment["chat_messages"] = json.loads(segment.get("chat_messages") or "[]")
    segment["tag_feedback"] = tag_feedback if tag_feedback is not None else _tag_feedback_by_segment([segment["id"]]).get(segment["id"], {})
    segment["censor_profanity"] = bool(segment.get("censor_profanity"))
    segment["remove_pauses"] = bool(segment.get("remove_pauses"))
    segment.pop("embedding", None)
    return segment


def serialize_segments(segments: list[dict]) -> list[dict]:
    feedback = _tag_feedback_by_segment([segment["id"] for segment in segments])
    return [serialize_segment(segment, feedback.get(segment["id"], {})) for segment in segments]
