from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.services.reference_queue import (
    claim_next,
    complete,
    decode_payload,
    enqueue,
    fail,
    heartbeat,
    recover_abandoned,
    release_expired_leases,
    request_cancel,
)


NOW = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)


SCHEMA = """
CREATE TABLE reference_imports (
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL,
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
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def memory_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def test_enqueue_preserves_folder_and_url_semantics_and_is_idempotent() -> None:
    connection = memory_connection()
    first = enqueue(
        connection,
        collection_id="collection",
        kind="folder",
        source=r"C:\clips",
        include_subfolders=True,
        payload={"source_id": "saved-folder"},
        import_id="folder-one",
        now=NOW,
    )
    duplicate = enqueue(
        connection,
        collection_id="collection",
        kind="folder",
        source=r"C:\clips",
        include_subfolders=False,
        import_id="folder-two",
        now=NOW,
    )
    remote = enqueue(
        connection,
        collection_id="collection",
        kind="url",
        source="https://youtu.be/example",
        include_subfolders=True,
        import_id="url-one",
        now=NOW,
    )

    assert first["id"] == duplicate["id"] == "folder-one"
    assert first["include_subfolders"] == 1
    assert decode_payload(first) == {"source_id": "saved-folder"}
    assert remote["kind"] == "url"
    assert remote["folder_path"] == "https://youtu.be/example"
    assert remote["include_subfolders"] == 0
    assert connection.execute("SELECT count(*) FROM reference_imports").fetchone()[0] == 2


def test_enqueue_rejects_unknown_kind() -> None:
    connection = memory_connection()
    with pytest.raises(ValueError, match="kind must be one of"):
        enqueue(connection, collection_id="collection", kind="stream", source="source", now=NOW)


def test_two_workers_cannot_claim_the_same_import(tmp_path) -> None:
    database = tmp_path / "reference-queue.sqlite3"
    setup = sqlite3.connect(database)
    setup.executescript(SCHEMA)
    enqueue(setup, collection_id="collection", kind="folder", source=r"C:\clips", import_id="job", now=NOW)
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


def test_claim_filter_heartbeat_counters_and_lease_guard() -> None:
    connection = memory_connection()
    enqueue(connection, collection_id="one", kind="folder", source=r"C:\one", import_id="folder", now=NOW)
    enqueue(connection, collection_id="two", kind="url", source="https://example.test/v", import_id="url", now=NOW)

    claimed = claim_next(connection, worker_id="worker", kinds=("url",), now=NOW, lease_seconds=10)
    assert claimed is not None and claimed["id"] == "url"
    assert heartbeat(connection, "url", "stale", progress=20, now=NOW) is None

    renewed = heartbeat(
        connection,
        "url",
        claimed["lease_token"],
        progress=45,
        message="Transcribing",
        total_files=1,
        imported_files=0,
        now=NOW + timedelta(seconds=1),
    )
    assert renewed is not None
    assert renewed["progress"] == 45
    assert renewed["message"] == "Transcribing"
    assert renewed["total_files"] == 1

    finished = complete(
        connection,
        "url",
        claimed["lease_token"],
        total_files=1,
        imported_files=1,
        now=NOW + timedelta(seconds=2),
    )
    assert finished is not None
    assert finished["state"] == "completed"
    assert finished["progress"] == 100
    assert finished["imported_files"] == 1


def test_expired_lease_requeues_and_old_owner_cannot_finish() -> None:
    connection = memory_connection()
    enqueue(connection, collection_id="collection", kind="folder", source=r"C:\clips", import_id="job", now=NOW)
    first = claim_next(connection, worker_id="old", now=NOW, lease_seconds=5)
    assert first is not None

    released = release_expired_leases(connection, now=NOW + timedelta(seconds=6))
    assert [item["id"] for item in released] == ["job"]
    assert released[0]["state"] == "queued"

    second = claim_next(connection, worker_id="new", now=NOW + timedelta(seconds=6))
    assert second is not None
    assert complete(connection, "job", first["lease_token"], now=NOW + timedelta(seconds=7)) is None
    assert complete(connection, "job", second["lease_token"], now=NOW + timedelta(seconds=7))["state"] == "completed"


def test_failure_retries_with_backoff_then_becomes_terminal() -> None:
    connection = memory_connection()
    enqueue(
        connection,
        collection_id="collection",
        kind="url",
        source="https://example.test/v",
        import_id="job",
        max_attempts=2,
        now=NOW,
    )
    first = claim_next(connection, worker_id="worker", now=NOW)
    assert first is not None
    heartbeat(connection, "job", first["lease_token"], progress=68, now=NOW)
    retry = fail(connection, "job", first["lease_token"], "network", base_backoff_seconds=10, now=NOW)
    assert retry is not None and retry["state"] == "queued"
    assert retry["progress"] == 0
    assert retry["last_error"] == ""
    assert claim_next(connection, worker_id="early", now=NOW + timedelta(seconds=9)) is None

    second = claim_next(connection, worker_id="worker", now=NOW + timedelta(seconds=10))
    assert second is not None and second["attempt_count"] == 2
    terminal = fail(connection, "job", second["lease_token"], "still unavailable", now=NOW + timedelta(seconds=11))
    assert terminal is not None and terminal["state"] == "failed"


def test_cancel_queued_and_running_imports() -> None:
    connection = memory_connection()
    enqueue(connection, collection_id="one", kind="folder", source=r"C:\one", import_id="queued", now=NOW)
    assert request_cancel(connection, "queued", now=NOW)["state"] == "cancelled"

    enqueue(connection, collection_id="two", kind="folder", source=r"C:\two", import_id="running", now=NOW)
    running = claim_next(connection, worker_id="worker", now=NOW)
    assert running is not None
    requested = request_cancel(connection, "running", now=NOW + timedelta(seconds=1))
    assert requested is not None and requested["cancel_requested"] == 1
    seen = heartbeat(connection, "running", running["lease_token"], now=NOW + timedelta(seconds=2))
    assert seen is not None and seen["cancel_requested"] == 1
    cancelled = complete(connection, "running", running["lease_token"], now=NOW + timedelta(seconds=3))
    assert cancelled is not None and cancelled["state"] == "cancelled"


def test_exclusive_startup_recovery_requeues_running_imports_immediately() -> None:
    connection = memory_connection()
    enqueue(connection, collection_id="one", kind="folder", source=r"C:\one", import_id="retry", now=NOW)
    enqueue(
        connection,
        collection_id="two",
        kind="url",
        source="https://example.test/v",
        import_id="final",
        max_attempts=1,
        now=NOW,
    )
    assert claim_next(connection, worker_id="old", kinds=("folder",), now=NOW, lease_seconds=3600) is not None
    assert claim_next(connection, worker_id="old", kinds=("url",), now=NOW, lease_seconds=3600) is not None

    recovered = recover_abandoned(connection, now=NOW + timedelta(seconds=1))
    by_id = {item["id"]: item for item in recovered}
    assert by_id["retry"]["state"] == "queued"
    assert by_id["final"]["state"] == "queued"
    assert by_id["final"]["attempt_count"] == 0
    assert by_id["retry"]["lease_owner"] is None


def test_permanent_reference_failure_does_not_retry() -> None:
    connection = memory_connection()
    enqueue(
        connection, collection_id="one", kind="folder", source=r"C:\missing",
        import_id="permanent", now=NOW,
    )
    claimed = claim_next(connection, worker_id="worker", now=NOW)
    terminal = fail(
        connection, "permanent", claimed["lease_token"], "folder missing",
        retryable=False, now=NOW,
    )
    assert terminal is not None and terminal["state"] == "failed"


def test_startup_recovery_keeps_only_one_active_import_per_collection() -> None:
    connection = memory_connection()
    enqueue(
        connection, collection_id="collection", kind="folder", source=r"C:\old",
        import_id="old", now=NOW,
    )
    connection.execute("UPDATE reference_imports SET state='interrupted' WHERE id='old'")
    enqueue(
        connection, collection_id="collection", kind="folder", source=r"C:\old",
        import_id="newer-interrupted", now=NOW + timedelta(seconds=1),
    )
    connection.execute(
        "UPDATE reference_imports SET state='interrupted' WHERE id='newer-interrupted'"
    )
    enqueue(
        connection, collection_id="collection", kind="folder", source=r"C:\old",
        import_id="already-queued", now=NOW + timedelta(seconds=2),
    )

    recover_abandoned(connection, now=NOW + timedelta(seconds=3))
    states = {
        row["id"]: row["state"]
        for row in connection.execute(
            "SELECT id, state FROM reference_imports WHERE collection_id='collection'"
        )
    }
    assert states == {
        "old": "cancelled",
        "newer-interrupted": "cancelled",
        "already-queued": "queued",
    }


def test_startup_recovery_uses_newest_interrupted_import_without_active_one() -> None:
    connection = memory_connection()
    enqueue(
        connection, collection_id="collection", kind="folder", source=r"C:\old",
        import_id="old", now=NOW,
    )
    connection.execute("UPDATE reference_imports SET state='interrupted' WHERE id='old'")
    enqueue(
        connection, collection_id="collection", kind="folder", source=r"C:\old",
        import_id="new", now=NOW + timedelta(seconds=1),
    )
    connection.execute("UPDATE reference_imports SET state='interrupted' WHERE id='new'")

    recover_abandoned(connection, now=NOW + timedelta(seconds=2))
    states = {
        row["id"]: row["state"]
        for row in connection.execute(
            "SELECT id, state FROM reference_imports WHERE collection_id='collection'"
        )
    }
    assert states == {"old": "cancelled", "new": "queued"}


def test_startup_recovery_keeps_different_sources_in_one_collection() -> None:
    connection = memory_connection()
    enqueue(
        connection, collection_id="collection", kind="folder", source=r"C:\one",
        import_id="folder", now=NOW,
    )
    enqueue(
        connection, collection_id="collection", kind="url", source="https://example.test/two",
        import_id="url", now=NOW + timedelta(seconds=1),
    )
    connection.execute("UPDATE reference_imports SET state='interrupted'")

    recover_abandoned(connection, now=NOW + timedelta(seconds=2))
    states = {
        row["id"]: row["state"]
        for row in connection.execute("SELECT id, state FROM reference_imports")
    }
    assert states == {"folder": "queued", "url": "queued"}


def test_repeated_reference_recovery_does_not_consume_attempt_budget() -> None:
    connection = memory_connection()
    enqueue(
        connection, collection_id="collection", kind="folder", source=r"C:\clips",
        import_id="job", max_attempts=1, now=NOW,
    )

    for offset in range(3):
        claimed = claim_next(connection, worker_id=f"worker-{offset}", now=NOW + timedelta(seconds=offset * 2))
        assert claimed is not None and claimed["attempt_count"] == 1
        heartbeat(
            connection, "job", claimed["lease_token"], progress=70,
            now=NOW + timedelta(seconds=offset * 2 + 1),
        )
        recovered = recover_abandoned(connection, now=NOW + timedelta(seconds=offset * 2 + 1))
        assert recovered[-1]["state"] == "queued"
        assert recovered[-1]["attempt_count"] == 0
        assert recovered[-1]["progress"] == 0
        assert recovered[-1]["last_error"] == ""

    assert claim_next(connection, worker_id="final", now=NOW + timedelta(seconds=10)) is not None
