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


def _job(connection: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    return _row(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)))


@contextmanager
def _write_transaction(connection: sqlite3.Connection):
    """Open an immediate transaction without taking ownership of an outer one."""

    if connection.in_transaction:
        savepoint = f"job_queue_{uuid.uuid4().hex}"
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
    video_id: str,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    job_id: str | None = None,
    max_attempts: int = 3,
    available_at: datetime | str | None = None,
    message: str = "Queued",
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Enqueue work, or return the already active job for ``video_id``.

    The lookup and insert share an IMMEDIATE transaction, so two application
    processes cannot enqueue two active copies merely by racing each other.
    A partial unique index in the database remains useful as a second line of
    defence, but is not required by this service.
    """

    normalized_kind = kind.strip()
    if not normalized_kind:
        raise ValueError("kind must not be empty")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    timestamp = _utc_iso(now)
    first_available_at = _utc_iso(available_at or timestamp)
    payload_json = json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    new_job_id = job_id or str(uuid.uuid4())

    with _write_transaction(connection):
        existing = _row(
            connection.execute(
                """SELECT * FROM jobs
                   WHERE video_id=? AND state IN ('queued', 'running')
                   ORDER BY created_at, id
                   LIMIT 1""",
                (video_id,),
            )
        )
        if existing is not None:
            return existing

        connection.execute(
            """INSERT INTO jobs (
                   id, video_id, kind, payload_json, state, progress, message,
                   attempt_count, max_attempts, available_at, lease_owner,
                   lease_expires_at, last_error, cancel_requested,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'queued', 0, ?, 0, ?, ?, NULL, NULL, '', 0, ?, ?)""",
            (
                new_job_id,
                video_id,
                normalized_kind,
                payload_json,
                message,
                max_attempts,
                first_available_at,
                timestamp,
                timestamp,
            ),
        )
        created = _job(connection, new_job_id)
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
    """Atomically claim the oldest available job and return its lease token."""

    if not worker_id.strip():
        raise ValueError("worker_id must not be empty")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    timestamp_dt = _utc_datetime(now)
    timestamp = timestamp_dt.isoformat()
    lease_expires_at = (timestamp_dt + timedelta(seconds=lease_seconds)).isoformat()
    lease_token = f"{worker_id.strip()}:{uuid.uuid4().hex}"
    normalized_kinds = tuple(dict.fromkeys(value.strip() for value in (kinds or ()) if value.strip()))

    where = [
        "state='queued'",
        "cancel_requested=0",
        "pause_requested=0",
        "attempt_count < max_attempts",
        "julianday(available_at) <= julianday(?)",
    ]
    parameters: list[Any] = [timestamp]
    if normalized_kinds:
        placeholders = ",".join("?" for _ in normalized_kinds)
        where.append(f"kind IN ({placeholders})")
        parameters.extend(normalized_kinds)

    with _write_transaction(connection):
        candidate = _row(
            connection.execute(
                f"""SELECT * FROM jobs
                    WHERE {' AND '.join(where)}
                    ORDER BY available_at, created_at, id
                    LIMIT 1""",
                parameters,
            )
        )
        if candidate is None:
            return None

        cursor = connection.execute(
            """UPDATE jobs
               SET state='running', attempt_count=attempt_count+1,
                   last_error='', lease_owner=?,
                   lease_expires_at=?, updated_at=?
               WHERE id=? AND state='queued' AND cancel_requested=0 AND pause_requested=0
                   AND attempt_count < max_attempts""",
            (lease_token, lease_expires_at, timestamp, candidate["id"]),
        )
        if cursor.rowcount != 1:
            return None
        claimed = _job(connection, str(candidate["id"]))
        assert claimed is not None
        claimed["lease_token"] = lease_token
        claimed["worker_id"] = worker_id.strip()
        return claimed


def heartbeat(
    connection: sqlite3.Connection,
    job_id: str,
    lease_token: str,
    *,
    lease_seconds: float = 60.0,
    progress: int | None = None,
    message: str | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Renew a lease and optionally publish progress.

    ``None`` means the lease is no longer owned by the caller.  The returned
    row exposes ``cancel_requested`` so workers can stop cooperatively.
    """

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
    parameters.extend((job_id, lease_token))

    with _write_transaction(connection):
        cursor = connection.execute(
            f"""UPDATE jobs SET {', '.join(assignments)}
                WHERE id=? AND state='running' AND lease_owner=?""",
            parameters,
        )
        return _job(connection, job_id) if cursor.rowcount == 1 else None


def update_progress(
    connection: sqlite3.Connection,
    job_id: str,
    lease_token: str,
    progress: int,
    message: str | None = None,
    *,
    lease_seconds: float = 60.0,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    return heartbeat(
        connection,
        job_id,
        lease_token,
        lease_seconds=lease_seconds,
        progress=progress,
        message=message,
        now=now,
    )


def complete(
    connection: sqlite3.Connection,
    job_id: str,
    lease_token: str,
    *,
    message: str = "Completed",
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Finish owned work.  A concurrent cancellation request always wins."""

    timestamp = _utc_iso(now)
    with _write_transaction(connection):
        cursor = connection.execute(
            """UPDATE jobs
               SET state=CASE
                       WHEN cancel_requested=1 THEN 'cancelled'
                       WHEN pause_requested=1 THEN 'paused'
                       ELSE 'completed'
                   END,
                   progress=CASE WHEN cancel_requested=1 OR pause_requested=1 THEN progress ELSE 100 END,
                   message=CASE
                       WHEN cancel_requested=1 THEN 'Cancelled'
                       WHEN pause_requested=1 THEN 'Paused'
                       ELSE ?
                   END,
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND state='running' AND lease_owner=?""",
            (message, timestamp, job_id, lease_token),
        )
        return _job(connection, job_id) if cursor.rowcount == 1 else None


def fail(
    connection: sqlite3.Connection,
    job_id: str,
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
        current = _job(connection, job_id)
        if current is None or current["state"] != "running" or current["lease_owner"] != lease_token:
            return None

        if int(current["cancel_requested"]):
            state, available_at, message = "cancelled", current["available_at"], "Cancelled"
        elif int(current.get("pause_requested") or 0):
            state, available_at, message = "paused", current["available_at"], "Paused"
        elif not retryable or int(current["attempt_count"]) >= int(current["max_attempts"]):
            state, available_at, message = "failed", current["available_at"], error
        else:
            exponent = max(0, int(current["attempt_count"]) - 1)
            delay = min(max_backoff_seconds, base_backoff_seconds * (2**exponent))
            state = "queued"
            available_at = (timestamp_dt + timedelta(seconds=delay)).isoformat()
            message = f"Retry scheduled: {error}"

        retry_progress = 0 if state == "queued" else int(current["progress"])
        retained_error = "" if state in {"queued", "paused"} else error

        connection.execute(
            """UPDATE jobs
               SET state=?, progress=?, available_at=?, message=?, last_error=?,
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND state='running' AND lease_owner=?""",
            (state, retry_progress, available_at, message, retained_error, timestamp, job_id, lease_token),
        )
        return _job(connection, job_id)


def release_expired_leases(
    connection: sqlite3.Connection,
    *,
    now: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Recover abandoned work and return every affected job."""

    timestamp = _utc_iso(now)
    released_ids: list[str] = []
    with _write_transaction(connection):
        expired = connection.execute(
            """SELECT id, attempt_count, max_attempts, cancel_requested
               FROM jobs
               WHERE state='running' AND lease_expires_at IS NOT NULL
                   AND julianday(lease_expires_at) <= julianday(?)
               ORDER BY lease_expires_at, created_at, id""",
            (timestamp,),
        ).fetchall()
        for raw in expired:
            if isinstance(raw, sqlite3.Row):
                value = dict(raw)
            else:
                value = dict(zip(("id", "attempt_count", "max_attempts", "cancel_requested"), raw, strict=True))
            if int(value["cancel_requested"]):
                state, message = "cancelled", "Cancelled"
            elif int(value.get("pause_requested") or 0):
                state, message = "paused", "Paused before the application closed"
            elif int(value["attempt_count"]) >= int(value["max_attempts"]):
                state, message = "failed", "Worker lease expired after the final attempt"
            else:
                state, message = "queued", "Worker lease expired; retry queued"
            retry_progress = 0 if state == "queued" else None
            retained_error = "" if state in {"queued", "paused"} else "Worker lease expired"
            connection.execute(
                """UPDATE jobs
                   SET state=?, progress=COALESCE(?, progress), message=?, last_error=?,
                       available_at=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND state='running'""",
                (state, retry_progress, message, retained_error, timestamp, timestamp, value["id"]),
            )
            released_ids.append(str(value["id"]))
        return [job for job_id in released_ids if (job := _job(connection, job_id)) is not None]


def recover_abandoned(
    connection: sqlite3.Connection,
    *,
    now: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Recover one newest/active row per video under the global lock.

    Older releases could leave several historical ``interrupted`` rows for a
    video.  Re-queueing all of them would analyse the same recording several
    times.  Keep one active (or, when none is active, newest interrupted) row
    and terminally cancel the rest.  An unfinished attempt belongs to the
    process that stopped, so startup recovery refunds that attempt; repeated
    orderly restarts therefore cannot exhaust the retry budget.
    """

    timestamp = _utc_iso(now)
    recovered_ids: list[str] = []
    with _write_transaction(connection):
        rows = connection.execute(
            """SELECT id, video_id, state, progress, attempt_count, max_attempts,
                      cancel_requested, pause_requested, created_at, updated_at
               FROM jobs
               WHERE state IN ('queued', 'running', 'interrupted')
               ORDER BY video_id,
                        CASE WHEN state IN ('queued', 'running') THEN 0 ELSE 1 END,
                        julianday(COALESCE(NULLIF(updated_at, ''), created_at)) DESC,
                        julianday(created_at) DESC,
                        id DESC"""
        ).fetchall()
        survivor_by_video: dict[str, str] = {}
        for raw in rows:
            value = dict(raw) if isinstance(raw, sqlite3.Row) else dict(
                zip(
                    (
                        "id", "video_id", "state", "progress", "attempt_count", "max_attempts",
                        "cancel_requested", "pause_requested", "created_at", "updated_at",
                    ),
                    raw,
                    strict=True,
                )
            )
            video_id = str(value["video_id"])
            survivor_id = survivor_by_video.setdefault(video_id, str(value["id"]))
            if str(value["id"]) != survivor_id:
                connection.execute(
                    """UPDATE jobs
                       SET state='cancelled', cancel_requested=1,
                           message='Superseded by a newer active job during recovery',
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
            elif int(value.get("pause_requested") or 0):
                state, message = "paused", "Paused before the application closed"
            elif refunded_attempts >= int(value["max_attempts"]):
                state, message = "failed", "Application restarted after the final attempt"
            else:
                state, message = "queued", "Application restarted; retry queued"
            retry_progress = 0 if state == "queued" else int(value["progress"])
            retained_error = "" if state in {"queued", "paused"} else "Application restarted"
            connection.execute(
                """UPDATE jobs
                   SET state=?, progress=?, attempt_count=?, message=?, last_error=?,
                       available_at=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND state IN ('running', 'interrupted')""",
                (
                    state, retry_progress, refunded_attempts, message, retained_error,
                    timestamp, timestamp, value["id"],
                ),
            )
            recovered_ids.append(str(value["id"]))
        return [item for job_id in recovered_ids if (item := _job(connection, job_id)) is not None]


def request_cancel(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Cancel queued work immediately or ask the current worker to stop."""

    timestamp = _utc_iso(now)
    with _write_transaction(connection):
        current = _job(connection, job_id)
        if current is None:
            return None
        if current["state"] == "queued":
            connection.execute(
                """UPDATE jobs
                   SET state='cancelled', cancel_requested=1, message='Cancelled',
                       lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND state='queued'""",
                (timestamp, job_id),
            )
        elif current["state"] == "running":
            connection.execute(
                """UPDATE jobs
                   SET cancel_requested=1, message='Cancellation requested', updated_at=?
                   WHERE id=? AND state='running'""",
                (timestamp, job_id),
            )
        return _job(connection, job_id)


def request_pause(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Persistently pause work without discarding its already cached stages."""

    timestamp = _utc_iso(now)
    with _write_transaction(connection):
        current = _job(connection, job_id)
        if current is None:
            return None
        if current["state"] == "queued":
            connection.execute(
                """UPDATE jobs
                   SET state='paused', pause_requested=1, message='Paused',
                       lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND state='queued'""",
                (timestamp, job_id),
            )
        elif current["state"] == "running":
            connection.execute(
                """UPDATE jobs
                   SET pause_requested=1, message='Pause requested', updated_at=?
                   WHERE id=? AND state='running'""",
                (timestamp, job_id),
            )
        return _job(connection, job_id)


def resume(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Place a paused job back in the durable queue, retaining its progress."""

    timestamp = _utc_iso(now)
    with _write_transaction(connection):
        current = _job(connection, job_id)
        if current is None:
            return None
        if current["state"] == "paused":
            connection.execute(
                """UPDATE jobs
                   SET state='queued', pause_requested=0, cancel_requested=0,
                       available_at=?, message='Resume queued', last_error='',
                       lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND state='paused'""",
                (timestamp, timestamp, job_id),
            )
        return _job(connection, job_id)


def get_job(connection: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    return _job(connection, job_id)


def decode_payload(job: Mapping[str, Any]) -> dict[str, Any]:
    raw = job.get("payload_json") or "{}"
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
