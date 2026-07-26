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
                reading_likelihood REAL NOT NULL DEFAULT 0,
                audio_event_score INTEGER NOT NULL DEFAULT 0,
                vision_score INTEGER NOT NULL DEFAULT 0,
                duplicate_group TEXT NOT NULL DEFAULT '',
                censor_profanity INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_segments_video_time ON segments(video_id, start_seconds);
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
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS caption_favorites (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                captions_preset TEXT NOT NULL,
                base_color TEXT NOT NULL,
                active_color TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS export_defaults (
                id INTEGER PRIMARY KEY CHECK(id=1),
                layout TEXT NOT NULL DEFAULT 'original',
                audio_track INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
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
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS boss_profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                sound_path TEXT NOT NULL,
                crop_x REAL NOT NULL DEFAULT 10,
                crop_y REAL NOT NULL DEFAULT 74,
                crop_width REAL NOT NULL DEFAULT 80,
                crop_height REAL NOT NULL DEFAULT 16,
                threshold REAL NOT NULL DEFAULT 0.72,
                minimum_gap_seconds REAL NOT NULL DEFAULT 4,
                audio_track INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS boss_reports (
                id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                profile_id TEXT NOT NULL REFERENCES boss_profiles(id) ON DELETE CASCADE,
                state TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                audio_track INTEGER NOT NULL DEFAULT 1,
                result_path TEXT,
                event_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
        if "review_reason" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN review_reason TEXT NOT NULL DEFAULT ''")
        if "quality_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN quality_score INTEGER NOT NULL DEFAULT 0")
        if "quality_signals" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN quality_signals TEXT NOT NULL DEFAULT '[]'")
        if "reading_likelihood" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN reading_likelihood REAL NOT NULL DEFAULT 0")
        if "audio_event_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN audio_event_score INTEGER NOT NULL DEFAULT 0")
        if "vision_score" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN vision_score INTEGER NOT NULL DEFAULT 0")
        if "duplicate_group" not in columns:
            con.execute("ALTER TABLE segments ADD COLUMN duplicate_group TEXT NOT NULL DEFAULT ''")
        video_columns = {item["name"] for item in con.execute("PRAGMA table_info(videos)").fetchall()}
        if "source_url" not in video_columns:
            con.execute("ALTER TABLE videos ADD COLUMN source_url TEXT")
        if "transcript_audio_track" not in video_columns:
            con.execute("ALTER TABLE videos ADD COLUMN transcript_audio_track INTEGER NOT NULL DEFAULT 1")
        if "audio_analysis_mode" not in video_columns:
            con.execute("ALTER TABLE videos ADD COLUMN audio_analysis_mode TEXT NOT NULL DEFAULT 'single'")
        boss_profile_columns = {item["name"] for item in con.execute("PRAGMA table_info(boss_profiles)").fetchall()}
        if "audio_track" not in boss_profile_columns:
            con.execute("ALTER TABLE boss_profiles ADD COLUMN audio_track INTEGER NOT NULL DEFAULT 1")
        boss_report_columns = {item["name"] for item in con.execute("PRAGMA table_info(boss_reports)").fetchall()}
        if "audio_track" not in boss_report_columns:
            con.execute("ALTER TABLE boss_reports ADD COLUMN audio_track INTEGER NOT NULL DEFAULT 1")
        con.execute(
            "INSERT OR IGNORE INTO caption_defaults (id, captions_preset, base_color, active_color, updated_at) VALUES (1, 'highlight', '#FFFFFF', '#FFFF00', ?)",
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
        con.execute("UPDATE boss_reports SET state='interrupted', message='Server was restarted', updated_at=? WHERE state IN ('queued', 'running')", (now(),))


def rows(query: str, parameters: tuple = ()) -> list[dict]:
    with connection() as con:
        return [dict(row) for row in con.execute(query, parameters).fetchall()]


def row(query: str, parameters: tuple = ()) -> dict | None:
    result = rows(query, parameters)
    return result[0] if result else None


def serialize_segment(segment: dict) -> dict:
    segment["keywords"] = json.loads(segment["keywords"])
    segment["tags"] = json.loads(segment.get("tags") or "[]")
    segment["word_timestamps"] = json.loads(segment.get("word_timestamps") or "[]")
    segment["quality_signals"] = json.loads(segment.get("quality_signals") or "[]")
    segment["censor_profanity"] = bool(segment.get("censor_profanity"))
    segment.pop("embedding", None)
    return segment
