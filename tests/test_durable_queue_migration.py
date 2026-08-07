from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import database as db
from app.config import settings


def test_upgrade_preserves_queued_work_and_adds_durable_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "clipfinder_data_dir", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    timestamp = "2026-08-06T10:00:00+00:00"
    with sqlite3.connect(settings.db_path) as con:
        con.executescript(
            """
            CREATE TABLE videos (
                id TEXT PRIMARY KEY, original_name TEXT NOT NULL, path TEXT NOT NULL,
                status TEXT NOT NULL, error_message TEXT, duration_seconds REAL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, video_id TEXT NOT NULL, state TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE collections (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
            );
            CREATE TABLE reference_imports (
                id TEXT PRIMARY KEY, collection_id TEXT NOT NULL, folder_path TEXT NOT NULL,
                state TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '', total_files INTEGER NOT NULL DEFAULT 0,
                imported_files INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        con.execute(
            "INSERT INTO videos (id, original_name, path, status, created_at, updated_at) VALUES ('video', 'v.mp4', 'v.mp4', 'queued', ?, ?)",
            (timestamp, timestamp),
        )
        con.execute(
            "INSERT INTO jobs (id, video_id, state, message, created_at, updated_at) VALUES ('job', 'video', 'queued', 'Queued', ?, ?)",
            (timestamp, timestamp),
        )
        con.execute("INSERT INTO collections VALUES ('collection', 'test', ?)", (timestamp,))
        con.execute(
            "INSERT INTO reference_imports (id, collection_id, folder_path, state, message, created_at, updated_at) VALUES ('reference', 'collection', 'https://youtu.be/example', 'queued', 'Queued', ?, ?)",
            (timestamp, timestamp),
        )

    db.initialize()

    with sqlite3.connect(settings.db_path) as con:
        con.row_factory = sqlite3.Row
        job = con.execute("SELECT * FROM jobs WHERE id='job'").fetchone()
        reference = con.execute("SELECT * FROM reference_imports WHERE id='reference'").fetchone()
        assert job["state"] == "queued"
        assert job["available_at"] == timestamp
        assert job["kind"] == "analysis"
        assert reference["state"] == "queued"
        assert reference["available_at"] == timestamp
        assert reference["kind"] == "url"
        assert reference["include_subfolders"] == 0
        assert "lease_expires_at" in job.keys()
        assert "payload_json" in reference.keys()

