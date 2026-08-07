from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

from app.services.job_queue import (
    claim_next,
    complete,
    enqueue,
    fail,
    heartbeat,
    recover_abandoned,
    release_expired_leases,
    request_cancel,
    request_pause,
    resume,
)


NOW = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)


SCHEMA = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def memory_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def test_enqueue_is_idempotent_for_an_active_video() -> None:
    connection = memory_connection()
    first = enqueue(connection, video_id="video", kind="analysis", payload={"mode": "fast"}, job_id="one", now=NOW)
    duplicate = enqueue(connection, video_id="video", kind="analysis", payload={"mode": "extended"}, job_id="two", now=NOW)
    other_video = enqueue(connection, video_id="other-video", kind="remote-import", job_id="three", now=NOW)

    assert first["id"] == duplicate["id"] == "one"
    assert other_video["id"] == "three"
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2


def test_enqueue_does_not_overlap_remote_import_and_analysis_for_one_video() -> None:
    connection = memory_connection()
    remote_import = enqueue(connection, video_id="video", kind="remote_import", job_id="remote", now=NOW)
    analysis = enqueue(
        connection,
        video_id="video",
        kind="analysis",
        payload={"source": "downloaded-file"},
        job_id="analysis",
        now=NOW + timedelta(seconds=1),
    )

    assert analysis["id"] == remote_import["id"] == "remote"
    assert analysis["kind"] == "remote_import"
    assert connection.execute("SELECT count(*) FROM jobs WHERE video_id='video'").fetchone()[0] == 1


def test_two_workers_cannot_claim_the_same_job(tmp_path) -> None:
    database = tmp_path / "queue.sqlite3"
    setup = sqlite3.connect(database)
    setup.executescript(SCHEMA)
    enqueue(setup, video_id="video", kind="analysis", job_id="job", now=NOW)
    setup.close()

    barrier = threading.Barrier(2)
    results: list[dict | None] = []

    def worker(worker_id: str) -> None:
        connection = sqlite3.connect(database, timeout=5)
        connection.row_factory = sqlite3.Row
        barrier.wait()
        results.append(claim_next(connection, worker_id=worker_id, now=NOW))
        connection.close()

    threads = [threading.Thread(target=worker, args=(worker_id,)) for worker_id in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    assert claimed[0]["id"] == "job"
    assert claimed[0]["lease_token"].startswith(("a:", "b:"))


def test_claim_is_fifo_and_lease_token_guards_updates() -> None:
    connection = memory_connection()
    enqueue(connection, video_id="later", kind="analysis", job_id="later", now=NOW + timedelta(seconds=1))
    enqueue(connection, video_id="first", kind="analysis", job_id="first", now=NOW)

    claimed = claim_next(connection, worker_id="worker", now=NOW + timedelta(seconds=2), lease_seconds=10)
    assert claimed is not None and claimed["id"] == "first"
    assert heartbeat(connection, "first", "wrong-token", progress=40, now=NOW + timedelta(seconds=3)) is None

    renewed = heartbeat(
        connection,
        "first",
        claimed["lease_token"],
        progress=40,
        message="Working",
        now=NOW + timedelta(seconds=3),
    )
    assert renewed is not None
    assert renewed["progress"] == 40
    assert renewed["message"] == "Working"


def test_expired_lease_is_requeued_and_stale_token_cannot_complete() -> None:
    connection = memory_connection()
    enqueue(connection, video_id="video", kind="analysis", job_id="job", now=NOW)
    first = claim_next(connection, worker_id="old", now=NOW, lease_seconds=5)
    assert first is not None

    released = release_expired_leases(connection, now=NOW + timedelta(seconds=6))
    assert [job["id"] for job in released] == ["job"]
    assert released[0]["state"] == "queued"

    second = claim_next(connection, worker_id="new", now=NOW + timedelta(seconds=6))
    assert second is not None
    assert second["lease_token"] != first["lease_token"]
    assert complete(connection, "job", first["lease_token"], now=NOW + timedelta(seconds=7)) is None
    completed = complete(connection, "job", second["lease_token"], now=NOW + timedelta(seconds=7))
    assert completed is not None and completed["state"] == "completed" and completed["progress"] == 100


def test_failure_retries_with_backoff_until_max_attempts() -> None:
    connection = memory_connection()
    enqueue(connection, video_id="video", kind="analysis", job_id="job", max_attempts=2, now=NOW)

    first = claim_next(connection, worker_id="worker", now=NOW)
    assert first is not None and first["attempt_count"] == 1
    heartbeat(connection, "job", first["lease_token"], progress=73, now=NOW)
    retry = fail(connection, "job", first["lease_token"], "temporary", base_backoff_seconds=10, now=NOW)
    assert retry is not None and retry["state"] == "queued"
    assert retry["progress"] == 0
    assert retry["last_error"] == ""
    assert claim_next(connection, worker_id="early", now=NOW + timedelta(seconds=9)) is None

    second = claim_next(connection, worker_id="worker", now=NOW + timedelta(seconds=10))
    assert second is not None and second["attempt_count"] == 2
    terminal = fail(connection, "job", second["lease_token"], "permanent", now=NOW + timedelta(seconds=11))
    assert terminal is not None and terminal["state"] == "failed"
    assert claim_next(connection, worker_id="again", now=NOW + timedelta(hours=1)) is None


def test_cancel_queued_and_running_jobs() -> None:
    connection = memory_connection()
    enqueue(connection, video_id="queued-video", kind="analysis", job_id="queued", now=NOW)
    queued = request_cancel(connection, "queued", now=NOW)
    assert queued is not None and queued["state"] == "cancelled"
    assert claim_next(connection, worker_id="worker", now=NOW) is None

    enqueue(connection, video_id="running-video", kind="analysis", job_id="running", now=NOW)
    running = claim_next(connection, worker_id="worker", now=NOW)
    assert running is not None
    requested = request_cancel(connection, "running", now=NOW + timedelta(seconds=1))
    assert requested is not None and requested["state"] == "running" and requested["cancel_requested"] == 1

    seen = heartbeat(connection, "running", running["lease_token"], now=NOW + timedelta(seconds=2))
    assert seen is not None and seen["cancel_requested"] == 1
    cancelled = complete(connection, "running", running["lease_token"], now=NOW + timedelta(seconds=3))
    assert cancelled is not None and cancelled["state"] == "cancelled"


def test_pause_persists_across_restart_and_can_resume_without_resetting_progress() -> None:
    connection = memory_connection()
    enqueue(connection, video_id="video", kind="analysis", job_id="job", now=NOW)
    running = claim_next(connection, worker_id="worker", now=NOW)
    assert running is not None
    heartbeat(connection, "job", running["lease_token"], progress=62, now=NOW)

    requested = request_pause(connection, "job", now=NOW + timedelta(seconds=1))
    assert requested is not None
    assert requested["state"] == "running"
    assert requested["pause_requested"] == 1
    paused = complete(connection, "job", running["lease_token"], now=NOW + timedelta(seconds=2))
    assert paused is not None
    assert paused["state"] == "paused"
    assert paused["progress"] == 62

    # Paused jobs are deliberately excluded from startup recovery and claiming.
    assert recover_abandoned(connection, now=NOW + timedelta(seconds=3)) == []
    assert claim_next(connection, worker_id="other", now=NOW + timedelta(seconds=3)) is None

    resumed = resume(connection, "job", now=NOW + timedelta(seconds=4))
    assert resumed is not None
    assert resumed["state"] == "queued"
    assert resumed["progress"] == 62
    resumed_running = claim_next(connection, worker_id="other", now=NOW + timedelta(seconds=4))
    assert resumed_running is not None
    assert resumed_running["progress"] == 62


def test_permanent_failure_skips_retry_and_startup_recovers_legacy_interrupted() -> None:
    connection = memory_connection()
    enqueue(connection, video_id="permanent-video", kind="analysis", job_id="permanent", now=NOW)
    claimed = claim_next(connection, worker_id="worker", now=NOW)
    terminal = fail(connection, "permanent", claimed["lease_token"], "missing source", retryable=False, now=NOW)
    assert terminal is not None and terminal["state"] == "failed"

    enqueue(connection, video_id="legacy-video", kind="analysis", job_id="legacy", now=NOW)
    connection.execute("UPDATE jobs SET state='interrupted' WHERE id='legacy'")
    recovered = recover_abandoned(connection, now=NOW + timedelta(seconds=1))
    assert recovered[0]["id"] == "legacy"
    assert recovered[0]["state"] == "queued"


def test_startup_recovery_keeps_only_one_active_job_per_video() -> None:
    connection = memory_connection()
    enqueue(connection, video_id="video", kind="analysis", job_id="old", now=NOW)
    connection.execute(
        "UPDATE jobs SET state='interrupted', updated_at=? WHERE id='old'",
        ((NOW + timedelta(seconds=1)).isoformat(),),
    )
    enqueue(
        connection, video_id="video", kind="analysis", job_id="newer-interrupted",
        now=NOW + timedelta(seconds=2),
    )
    connection.execute(
        "UPDATE jobs SET state='interrupted', updated_at=? WHERE id='newer-interrupted'",
        ((NOW + timedelta(seconds=3)).isoformat(),),
    )
    enqueue(
        connection, video_id="video", kind="analysis", job_id="already-queued",
        now=NOW + timedelta(seconds=4),
    )

    recover_abandoned(connection, now=NOW + timedelta(seconds=5))
    states = {
        row["id"]: row["state"]
        for row in connection.execute("SELECT id, state FROM jobs WHERE video_id='video'")
    }
    assert states == {
        "old": "cancelled",
        "newer-interrupted": "cancelled",
        "already-queued": "queued",
    }
    assert connection.execute(
        "SELECT count(*) FROM jobs WHERE video_id='video' AND state IN ('queued', 'running')"
    ).fetchone()[0] == 1


def test_startup_recovery_uses_newest_interrupted_job_when_no_active_job_exists() -> None:
    connection = memory_connection()
    enqueue(connection, video_id="video", kind="analysis", job_id="old", now=NOW)
    connection.execute("UPDATE jobs SET state='interrupted' WHERE id='old'")
    enqueue(
        connection, video_id="video", kind="analysis", job_id="new",
        now=NOW + timedelta(seconds=1),
    )
    connection.execute("UPDATE jobs SET state='interrupted' WHERE id='new'")

    recover_abandoned(connection, now=NOW + timedelta(seconds=2))
    states = {
        row["id"]: row["state"]
        for row in connection.execute("SELECT id, state FROM jobs WHERE video_id='video'")
    }
    assert states == {"old": "cancelled", "new": "queued"}


def test_repeated_startup_recovery_does_not_consume_attempt_budget() -> None:
    connection = memory_connection()
    enqueue(
        connection, video_id="video", kind="analysis", job_id="job",
        max_attempts=1, now=NOW,
    )

    for offset in range(3):
        claimed = claim_next(connection, worker_id=f"worker-{offset}", now=NOW + timedelta(seconds=offset * 2))
        assert claimed is not None and claimed["attempt_count"] == 1
        heartbeat(
            connection, "job", claimed["lease_token"], progress=80,
            now=NOW + timedelta(seconds=offset * 2 + 1),
        )
        recovered = recover_abandoned(connection, now=NOW + timedelta(seconds=offset * 2 + 1))
        assert recovered[-1]["state"] == "queued"
        assert recovered[-1]["attempt_count"] == 0
        assert recovered[-1]["progress"] == 0
        assert recovered[-1]["last_error"] == ""

    assert claim_next(connection, worker_id="final", now=NOW + timedelta(seconds=10)) is not None
