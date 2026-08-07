from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any


ACTIVE_STATES = ("queued", "running")
TERMINAL_STATES = ("completed", "failed", "cancelled")
REFERENCE_KINDS = ("folder", "url")


def _utc_datetime(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _utc_iso(value: datetime | str | None = None) -> str:
    return _utc_datetime(value).isoformat()


def _row(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, sqlite3.Row):
        return dict(value)
    columns = [column[0] for column in cursor.description or ()]
    return dict(zip(columns, value, strict=True))


def _reference_import(connection: sqlite3.Connection, import_id: str) -> dict[str, Any] | None:
    return _row(connection.execute("SELECT * FROM reference_imports WHERE id=?", (import_id,)))


@contextmanager
def _write_transaction(connection: sqlite3.Connection):
    """Open an immediate transaction without taking ownership of an outer one."""

    if connection.in_transaction:
        savepoint = f"reference_queue_{uuid.uuid4().hex}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except BaseException:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE {savepoint}")
        return

    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def enqueue(
    connection: sqlite3.Connection,
    *,
    collection_id: str,
    kind: str,
    source: str,
    include_subfolders: bool = False,
    payload: Mapping[str, Any] | None = None,
    import_id: str | None = None,
    max_attempts: int = 3,
    available_at: datetime | str | None = None,
    message: str = "Queued",
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Persist a folder/URL import, or return its already-active duplicate.

    ``source`` is stored in the legacy ``folder_path`` column.  For ``folder``
    imports it is an absolute folder path; for ``url`` imports it is the
    normalized source URL.  Keeping this compatibility column avoids a table
    rebuild while ``kind`` makes the two meanings explicit.
    """

    normalized_collection_id = collection_id.strip()
    normalized_kind = kind.strip().lower()
    normalized_source = source.strip()
    if not normalized_collection_id:
        raise ValueError("collection_id must not be empty")
    if normalized_kind not in REFERENCE_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(REFERENCE_KINDS)}")
    if not normalized_source:
        raise ValueError("source must not be empty")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    timestamp = _utc_iso(now)
    first_available_at = _utc_iso(available_at or timestamp)
    payload_json = json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    new_import_id = import_id or str(uuid.uuid4())
    recursive = bool(include_subfolders) if normalized_kind == "folder" else False

    with _write_transaction(connection):
        existing = _row(
            connection.execute(
                """SELECT * FROM reference_imports
                   WHERE collection_id=? AND kind=? AND folder_path=?
                       AND state IN ('queued', 'running')
                   ORDER BY created_at, id
                   LIMIT 1""",
                (normalized_collection_id, normalized_kind, normalized_source),
            )
        )
        if existing is not None:
            return existing

        connection.execute(
            """INSERT INTO reference_imports (
                   id, collection_id, folder_path, kind, include_subfolders,
                   payload_json, state, progress, message, total_files,
                   imported_files, attempt_count, max_attempts, available_at,
                   lease_owner, lease_expires_at, last_error, cancel_requested,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, 0, 0, 0, ?, ?,
                         NULL, NULL, '', 0, ?, ?)""",
            (
                new_import_id,
                normalized_collection_id,
                normalized_source,
                normalized_kind,
                int(recursive),
                payload_json,
                message,
                max_attempts,
                first_available_at,
                timestamp,
                timestamp,
            ),
        )
        created = _reference_import(connection, new_import_id)
        assert created is not None
        return created


def claim_next(
    connection: sqlite3.Connection,
    *,
    worker_id: str,
    kinds: Iterable[str] | None = None,
    lease_seconds: float = 60.0,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Atomically claim the oldest available reference import."""

    normalized_worker_id = worker_id.strip()
    if not normalized_worker_id:
        raise ValueError("worker_id must not be empty")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    normalized_kinds = tuple(dict.fromkeys(value.strip().lower() for value in (kinds or ()) if value.strip()))
    unknown_kinds = set(normalized_kinds).difference(REFERENCE_KINDS)
    if unknown_kinds:
        raise ValueError(f"unsupported kinds: {', '.join(sorted(unknown_kinds))}")

    timestamp_dt = _utc_datetime(now)
    timestamp = timestamp_dt.isoformat()
    lease_expires_at = (timestamp_dt + timedelta(seconds=lease_seconds)).isoformat()
    lease_token = f"{normalized_worker_id}:{uuid.uuid4().hex}"

    where = [
        "state='queued'",
        "cancel_requested=0",
        "attempt_count < max_attempts",
        "julianday(COALESCE(NULLIF(available_at, ''), created_at)) <= julianday(?)",
    ]
    parameters: list[Any] = [timestamp]
    if normalized_kinds:
        placeholders = ",".join("?" for _ in normalized_kinds)
        where.append(f"kind IN ({placeholders})")
        parameters.extend(normalized_kinds)

    with _write_transaction(connection):
        candidate = _row(
            connection.execute(
                f"""SELECT * FROM reference_imports
                    WHERE {' AND '.join(where)}
                    ORDER BY COALESCE(NULLIF(available_at, ''), created_at), created_at, id
                    LIMIT 1""",
                parameters,
            )
        )
        if candidate is None:
            return None

        cursor = connection.execute(
            """UPDATE reference_imports
               SET state='running', attempt_count=attempt_count+1,
                   progress=0, last_error='', lease_owner=?,
                   lease_expires_at=?, updated_at=?
               WHERE id=? AND state='queued' AND cancel_requested=0
                   AND attempt_count < max_attempts""",
            (lease_token, lease_expires_at, timestamp, candidate["id"]),
        )
        if cursor.rowcount != 1:
            return None
        claimed = _reference_import(connection, str(candidate["id"]))
        assert claimed is not None
        claimed["lease_token"] = lease_token
        claimed["worker_id"] = normalized_worker_id
        return claimed


def heartbeat(
    connection: sqlite3.Connection,
    import_id: str,
    lease_token: str,
    *,
    lease_seconds: float = 60.0,
    progress: int | None = None,
    message: str | None = None,
    total_files: int | None = None,
    imported_files: int | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Renew a lease, publish progress and expose cancellation requests."""

    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    timestamp_dt = _utc_datetime(now)
    timestamp = timestamp_dt.isoformat()
    lease_expires_at = (timestamp_dt + timedelta(seconds=lease_seconds)).isoformat()

    assignments = ["lease_expires_at=?", "updated_at=?"]
    parameters: list[Any] = [lease_expires_at, timestamp]
    if progress is not None:
        assignments.append("progress=?")
        parameters.append(max(0, min(100, int(progress))))
    if message is not None:
        assignments.append("message=?")
        parameters.append(message)
    if total_files is not None:
        assignments.append("total_files=?")
        parameters.append(max(0, int(total_files)))
    if imported_files is not None:
        assignments.append("imported_files=?")
        parameters.append(max(0, int(imported_files)))
    parameters.extend((import_id, lease_token))

    with _write_transaction(connection):
        cursor = connection.execute(
            f"""UPDATE reference_imports SET {', '.join(assignments)}
                WHERE id=? AND state='running' AND lease_owner=?""",
            parameters,
        )
        return _reference_import(connection, import_id) if cursor.rowcount == 1 else None


def update_progress(
    connection: sqlite3.Connection,
    import_id: str,
    lease_token: str,
    progress: int,
    message: str | None = None,
    *,
    total_files: int | None = None,
    imported_files: int | None = None,
    lease_seconds: float = 60.0,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    return heartbeat(
        connection,
        import_id,
        lease_token,
        lease_seconds=lease_seconds,
        progress=progress,
        message=message,
        total_files=total_files,
        imported_files=imported_files,
        now=now,
    )


def complete(
    connection: sqlite3.Connection,
    import_id: str,
    lease_token: str,
    *,
    message: str = "Completed",
    total_files: int | None = None,
    imported_files: int | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Finish owned work.  A concurrent cancellation request always wins."""

    timestamp = _utc_iso(now)
    assignments = [
        "state=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'completed' END",
        "progress=CASE WHEN cancel_requested=1 THEN progress ELSE 100 END",
        "message=CASE WHEN cancel_requested=1 THEN 'Cancelled' ELSE ? END",
        "lease_owner=NULL",
        "lease_expires_at=NULL",
        "updated_at=?",
    ]
    parameters: list[Any] = [message, timestamp]
    if total_files is not None:
        assignments.append("total_files=?")
        parameters.append(max(0, int(total_files)))
    if imported_files is not None:
        assignments.append("imported_files=?")
        parameters.append(max(0, int(imported_files)))
    parameters.extend((import_id, lease_token))

    with _write_transaction(connection):
        cursor = connection.execute(
            f"""UPDATE reference_imports SET {', '.join(assignments)}
                WHERE id=? AND state='running' AND lease_owner=?""",
            parameters,
        )
        return _reference_import(connection, import_id) if cursor.rowcount == 1 else None


def fail(
    connection: sqlite3.Connection,
    import_id: str,
    lease_token: str,
    error: str,
    *,
    base_backoff_seconds: float = 5.0,
    max_backoff_seconds: float = 300.0,
    retryable: bool = True,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Fail owned work, scheduling a bounded exponential retry when allowed."""

    if base_backoff_seconds < 0 or max_backoff_seconds < 0:
        raise ValueError("backoff must not be negative")
    timestamp_dt = _utc_datetime(now)
    timestamp = timestamp_dt.isoformat()

    with _write_transaction(connection):
        current = _reference_import(connection, import_id)
        if current is None or current["state"] != "running" or current["lease_owner"] != lease_token:
            return None

        if int(current["cancel_requested"]):
            state, available_at, message = "cancelled", current["available_at"], "Cancelled"
        elif not retryable or int(current["attempt_count"]) >= int(current["max_attempts"]):
            state, available_at, message = "failed", current["available_at"], error
        else:
            exponent = max(0, int(current["attempt_count"]) - 1)
            delay = min(max_backoff_seconds, base_backoff_seconds * (2**exponent))
            state = "queued"
            available_at = (timestamp_dt + timedelta(seconds=delay)).isoformat()
            message = f"Retry scheduled: {error}"

        retry_progress = 0 if state == "queued" else int(current["progress"])
        retained_error = "" if state == "queued" else error

        connection.execute(
            """UPDATE reference_imports
               SET state=?, progress=?, available_at=?, message=?, last_error=?,
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND state='running' AND lease_owner=?""",
            (state, retry_progress, available_at, message, retained_error, timestamp, import_id, lease_token),
        )
        return _reference_import(connection, import_id)


def _recover_running(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    timestamp: str,
    reason: str,
) -> list[dict[str, Any]]:
    recovered_ids: list[str] = []
    for value in rows:
        if int(value["cancel_requested"]):
            state, message = "cancelled", "Cancelled"
        elif int(value["attempt_count"]) >= int(value["max_attempts"]):
            state, message = "failed", f"{reason} after the final attempt"
        else:
            state, message = "queued", f"{reason}; retry queued"
        retry_progress = 0 if state == "queued" else None
        retained_error = "" if state == "queued" else reason
        connection.execute(
            """UPDATE reference_imports
               SET state=?, progress=COALESCE(?, progress), message=?, last_error=?, available_at=?,
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND state='running'""",
            (state, retry_progress, message, retained_error, timestamp, timestamp, value["id"]),
        )
        recovered_ids.append(str(value["id"]))
    return [item for import_id in recovered_ids if (item := _reference_import(connection, import_id)) is not None]


def release_expired_leases(
    connection: sqlite3.Connection,
    *,
    now: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Recover reference imports whose worker lease has expired."""

    timestamp = _utc_iso(now)
    with _write_transaction(connection):
        raw_rows = connection.execute(
            """SELECT id, attempt_count, max_attempts, cancel_requested
               FROM reference_imports
               WHERE state='running' AND lease_expires_at IS NOT NULL
                   AND julianday(lease_expires_at) <= julianday(?)
               ORDER BY lease_expires_at, created_at, id""",
            (timestamp,),
        ).fetchall()
        columns = ("id", "attempt_count", "max_attempts", "cancel_requested")
        rows = [dict(raw) if isinstance(raw, sqlite3.Row) else dict(zip(columns, raw, strict=True)) for raw in raw_rows]
        return _recover_running(connection, rows, timestamp=timestamp, reason="Worker lease expired")


def recover_abandoned(
    connection: sqlite3.Connection,
    *,
    now: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Recover one newest/active copy of each reference import after restart.

    This intentionally ignores lease expiry.  Call it only while holding the
    application's single-instance/process lock, which proves no previous
    worker can still own the rows. Historical duplicates are terminally
    cancelled instead of all being imported again. The unfinished attempt is
    refunded so repeated application restarts cannot exhaust retry limits.
    """

    timestamp = _utc_iso(now)
    with _write_transaction(connection):
        raw_rows = connection.execute(
            """SELECT id, collection_id, kind, folder_path, state, progress, attempt_count,
                      max_attempts, cancel_requested, created_at, updated_at
               FROM reference_imports
               WHERE state IN ('running', 'interrupted')
                  OR state='queued'
               ORDER BY collection_id, kind, folder_path,
                        CASE WHEN state IN ('queued', 'running') THEN 0 ELSE 1 END,
                        julianday(COALESCE(NULLIF(updated_at, ''), created_at)) DESC,
                        julianday(created_at) DESC,
                        id DESC"""
        ).fetchall()
        columns = (
            "id", "collection_id", "kind", "folder_path", "state", "progress", "attempt_count",
            "max_attempts", "cancel_requested", "created_at", "updated_at",
        )
        rows = [dict(raw) if isinstance(raw, sqlite3.Row) else dict(zip(columns, raw, strict=True)) for raw in raw_rows]
        recovered_ids: list[str] = []
        survivor_by_source: dict[tuple[str, str, str], str] = {}
        for value in rows:
            source_key = (
                str(value["collection_id"]),
                str(value["kind"]),
                str(value["folder_path"]),
            )
            survivor_id = survivor_by_source.setdefault(source_key, str(value["id"]))
            if str(value["id"]) != survivor_id:
                connection.execute(
                    """UPDATE reference_imports
                       SET state='cancelled', cancel_requested=1,
                           message='Superseded by a newer active import during recovery',
                           last_error='Superseded during application restart recovery',
                           lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                       WHERE id=? AND state IN ('queued', 'running', 'interrupted')""",
                    (timestamp, value["id"]),
                )
                recovered_ids.append(str(value["id"]))
                continue

            if value["state"] == "queued":
                continue

            refunded_attempts = max(0, int(value["attempt_count"]) - 1)
            if int(value["cancel_requested"]):
                state, message = "cancelled", "Cancelled"
            elif refunded_attempts >= int(value["max_attempts"]):
                state, message = "failed", "Application restarted after the final attempt"
            else:
                state, message = "queued", "Application restarted; retry queued"
            retry_progress = 0 if state == "queued" else int(value["progress"])
            retained_error = "" if state == "queued" else "Application restarted"
            connection.execute(
                """UPDATE reference_imports
                   SET state=?, progress=?, attempt_count=?, message=?, last_error=?, available_at=?,
                       lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND state IN ('running', 'interrupted')""",
                (
                    state, retry_progress, refunded_attempts, message, retained_error,
                    timestamp, timestamp, value["id"],
                ),
            )
            recovered_ids.append(str(value["id"]))
        return [item for import_id in recovered_ids if (item := _reference_import(connection, import_id)) is not None]


def request_cancel(
    connection: sqlite3.Connection,
    import_id: str,
    *,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Cancel queued work immediately or ask its worker to stop."""

    timestamp = _utc_iso(now)
    with _write_transaction(connection):
        current = _reference_import(connection, import_id)
        if current is None:
            return None
        if current["state"] == "queued":
            connection.execute(
                """UPDATE reference_imports
                   SET state='cancelled', cancel_requested=1, message='Cancelled',
                       lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND state='queued'""",
                (timestamp, import_id),
            )
        elif current["state"] == "running":
            connection.execute(
                """UPDATE reference_imports
                   SET cancel_requested=1, message='Cancellation requested', updated_at=?
                   WHERE id=? AND state='running'""",
                (timestamp, import_id),
            )
        return _reference_import(connection, import_id)


def get_import(connection: sqlite3.Connection, import_id: str) -> dict[str, Any] | None:
    return _reference_import(connection, import_id)


def decode_payload(reference_import: Mapping[str, Any]) -> dict[str, Any]:
    """Return the optional job payload while tolerating legacy/empty rows."""

    raw = reference_import.get("payload_json") or "{}"
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
