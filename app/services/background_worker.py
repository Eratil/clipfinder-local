"""One durable, process-safe worker for ClipFinder's long-running tasks."""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.services.workspace_cleanup import LeaseAlreadyHeld, ScopedFileLease


class WorkCancelled(RuntimeError):
    """Cooperative cancellation requested by the user."""


class WorkPaused(RuntimeError):
    """Cooperative persistent pause requested by the user."""


class LeaseLost(RuntimeError):
    """The durable queue lease no longer belongs to this worker."""


class PermanentWorkError(RuntimeError):
    """A deterministic error which must not be retried automatically."""


ProgressCallback = Callable[[int, str], None]
CancellationProbe = Callable[[], bool]


@dataclass(frozen=True)
class QueueAdapter:
    """Database-specific operations used by the generic worker."""

    name: str
    claim: Callable[[str, float], dict | None]
    heartbeat: Callable[[str, str, float], dict | None]
    update_progress: Callable[[str, str, int, str, float], dict | None]
    complete: Callable[[str, str], dict | None]
    fail: Callable[[str, str, str, bool], dict | None]
    dispatch: Callable[[dict, ProgressCallback, CancellationProbe], None]
    recover: Callable[[], None]


class DurableBackgroundWorker:
    """Claim and execute jobs sequentially across all durable queues.

    SQLite leases make a crashed job recoverable.  A refreshed filesystem
    lease additionally enforces one heavy worker across two ClipFinder
    processes sharing the same data directory.
    """

    def __init__(
        self,
        adapters: list[QueueAdapter],
        *,
        lock_directory: Path,
        poll_seconds: float = 1.0,
        lease_seconds: float = 60.0,
        heartbeat_seconds: float = 10.0,
        process_lease_seconds: float = 30.0,
        adapter_error_backoff_seconds: float | None = None,
        adapter_error_backoff_max_seconds: float = 5.0,
        adapter_operation_attempts: int = 3,
        on_error: Callable[[str, BaseException], None] | None = None,
        on_acquired: Callable[[], None] | None = None,
    ) -> None:
        if not adapters:
            raise ValueError("At least one queue adapter is required")
        if min(poll_seconds, lease_seconds, heartbeat_seconds, process_lease_seconds) <= 0:
            raise ValueError("Worker timing values must be positive")
        if heartbeat_seconds >= lease_seconds:
            raise ValueError("Heartbeat must run more often than the job lease expires")
        error_backoff = (
            max(float(poll_seconds), 0.1)
            if adapter_error_backoff_seconds is None
            else float(adapter_error_backoff_seconds)
        )
        if error_backoff <= 0 or adapter_error_backoff_max_seconds <= 0:
            raise ValueError("Adapter error backoff values must be positive")
        if adapter_error_backoff_max_seconds < error_backoff:
            raise ValueError("Maximum adapter error backoff cannot be shorter than its base")
        if adapter_operation_attempts <= 0:
            raise ValueError("Adapter operation attempts must be positive")
        self.adapters = tuple(adapters)
        self.lock_directory = Path(lock_directory)
        self.poll_seconds = float(poll_seconds)
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.process_lease_seconds = float(process_lease_seconds)
        self.adapter_error_backoff_seconds = error_backoff
        self.adapter_error_backoff_max_seconds = float(adapter_error_backoff_max_seconds)
        self.adapter_operation_attempts = int(adapter_operation_attempts)
        self.on_error = on_error or (lambda _context, _exc: None)
        self.on_acquired = on_acquired
        self.worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._lease: ScopedFileLease | None = None
        self._lease_guard = threading.Lock()
        self._adapter_error_guard = threading.Lock()
        self._adapter_error_streaks: dict[tuple[str, str], int] = {}
        self._source_offset = 0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="ClipFinder durable worker",
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop claiming new work and wait briefly for the current call."""
        self._stopping.set()
        self._wake.set()
        thread = self._thread
        if thread:
            thread.join(timeout=max(0.0, timeout))
        return not self.running

    def _report_error(self, context: str, exc: BaseException) -> None:
        """Report diagnostics without letting a faulty callback kill the worker."""
        try:
            self.on_error(context, exc)
        except BaseException:
            # Logging/reporting is deliberately best-effort. A custom error
            # sink must never become another single point of failure.
            pass

    def _note_adapter_success(self, adapter: QueueAdapter, operation: str) -> None:
        with self._adapter_error_guard:
            self._adapter_error_streaks.pop((adapter.name, operation), None)

    def _note_adapter_failure(
        self,
        adapter: QueueAdapter,
        operation: str,
        exc: BaseException,
    ) -> int:
        key = (adapter.name, operation)
        with self._adapter_error_guard:
            streak = self._adapter_error_streaks.get(key, 0) + 1
            self._adapter_error_streaks[key] = streak
        self._report_error(f"{operation} for {adapter.name} queue", exc)
        return streak

    def _wait_after_adapter_error(self, streak: int) -> None:
        exponent = min(max(0, int(streak) - 1), 8)
        delay = min(
            self.adapter_error_backoff_max_seconds,
            self.adapter_error_backoff_seconds * (2**exponent),
        )
        self._stopping.wait(delay)

    def _call_adapter(
        self,
        adapter: QueueAdapter,
        operation: str,
        call: Callable[[], object],
        *,
        attempts: int | None = None,
        wait_between_attempts: bool = True,
    ) -> tuple[bool, object | None]:
        """Call an adapter boundary without allowing it to terminate the loop."""
        limit = self.adapter_operation_attempts if attempts is None else max(1, attempts)
        for attempt in range(limit):
            try:
                result = call()
            except BaseException as exc:
                streak = self._note_adapter_failure(adapter, operation, exc)
                if attempt + 1 < limit and wait_between_attempts and not self._stopping.is_set():
                    self._wait_after_adapter_error(streak)
                continue
            self._note_adapter_success(adapter, operation)
            return True, result
        return False, None

    def _acquire_process_lease(self) -> bool:
        with self._lease_guard:
            if self._lease and self._lease.acquired:
                return True
            lease = ScopedFileLease(
                self.lock_directory,
                "durable-background-worker",
                owner=self.worker_id,
                stale_after_seconds=self.process_lease_seconds,
            )
            try:
                lease.acquire()
            except LeaseAlreadyHeld:
                return False
            except (OSError, ValueError) as exc:
                self._report_error("Acquiring process-wide worker lease", exc)
                return False
            self._lease = lease
            return True

    def _refresh_process_lease(self) -> bool:
        with self._lease_guard:
            if not self._lease:
                return False
            try:
                return self._lease.refresh()
            except BaseException as exc:
                # Antivirus/indexing can briefly lock the small file on
                # Windows. Ownership is still valid; the next heartbeat will
                # retry before the stale window elapses.
                self._report_error("Refreshing process-wide worker lease", exc)
                return self._lease.acquired

    def _release_process_lease(self) -> None:
        with self._lease_guard:
            if self._lease:
                try:
                    self._lease.release()
                except BaseException as exc:
                    self._report_error("Releasing process-wide worker lease", exc)
            self._lease = None

    def _recover(self) -> None:
        for adapter in self.adapters:
            try:
                adapter.recover()
            except BaseException as exc:
                self._report_error(f"Recovering {adapter.name} queue", exc)

    def _claim(self) -> tuple[QueueAdapter, dict] | None:
        count = len(self.adapters)
        failed_streaks: list[int] = []
        for shift in range(count):
            index = (self._source_offset + shift) % count
            adapter = self.adapters[index]
            try:
                job = adapter.claim(self.worker_id, self.lease_seconds)
            except BaseException as exc:
                failed_streaks.append(self._note_adapter_failure(adapter, "Claiming work", exc))
                continue
            self._note_adapter_success(adapter, "Claiming work")
            if job is not None:
                self._source_offset = (index + 1) % count
                return adapter, job
        if failed_streaks:
            # Back off only when no healthy queue supplied work. This avoids
            # a broken adapter starving another independent queue.
            self._wait_after_adapter_error(max(failed_streaks))
        return None

    def _run(self) -> None:
        acquired_once = False
        try:
            while not self._stopping.is_set():
                if not self._acquire_process_lease():
                    self._wake.wait(self.poll_seconds)
                    self._wake.clear()
                    continue
                if not acquired_once:
                    self._recover()
                    if self.on_acquired:
                        try:
                            self.on_acquired()
                        except BaseException as exc:
                            self._report_error("Worker startup maintenance", exc)
                    acquired_once = True
                elif not self._refresh_process_lease():
                    acquired_once = False
                    continue

                claimed = self._claim()
                if claimed is None:
                    self._wake.wait(self.poll_seconds)
                    self._wake.clear()
                    continue
                try:
                    self._execute(*claimed)
                except BaseException as exc:
                    # Adapter implementations are external boundaries. Keep
                    # the durable loop alive even if a new call site escapes
                    # the narrower handling inside ``_execute``.
                    self._report_error("Unhandled durable worker execution error", exc)
                    self._wait_after_adapter_error(1)
        finally:
            self._release_process_lease()

    def _execute(self, adapter: QueueAdapter, job: dict) -> None:
        job_id = str(job["id"])
        lease_token = str(job.get("lease_token") or job.get("lease_owner") or "")
        heartbeat_done = threading.Event()
        cancel_requested = threading.Event()
        pause_requested = threading.Event()
        lease_lost = threading.Event()

        def heartbeat_loop() -> None:
            while not heartbeat_done.wait(self.heartbeat_seconds):
                try:
                    # The process-wide lock is independent from SQLite. Keep
                    # it live even if the database is temporarily locked.
                    self._refresh_process_lease()
                    current = adapter.heartbeat(job_id, lease_token, self.lease_seconds)
                except BaseException as exc:
                    self._note_adapter_failure(adapter, "Refreshing job lease", exc)
                    continue
                self._note_adapter_success(adapter, "Refreshing job lease")
                if current is None:
                    lease_lost.set()
                    return
                if int(current.get("cancel_requested") or 0):
                    cancel_requested.set()
                if int(current.get("pause_requested") or 0):
                    pause_requested.set()

        heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            daemon=True,
            name=f"ClipFinder heartbeat {adapter.name}",
        )
        heartbeat_thread.start()

        def should_cancel() -> bool:
            return cancel_requested.is_set() or pause_requested.is_set()

        def progress(value: int, message: str) -> None:
            if lease_lost.is_set():
                raise LeaseLost(f"Lease lost for {adapter.name} job {job_id}")
            if cancel_requested.is_set():
                raise WorkCancelled(f"Cancelled {adapter.name} job {job_id}")
            if pause_requested.is_set():
                raise WorkPaused(f"Paused {adapter.name} job {job_id}")
            ok, current = self._call_adapter(
                adapter,
                "Updating job progress",
                lambda: adapter.update_progress(
                    job_id,
                    lease_token,
                    value,
                    message,
                    self.lease_seconds,
                ),
            )
            if not ok:
                lease_lost.set()
                raise LeaseLost(f"Could not refresh {adapter.name} job {job_id}")
            if current is None:
                lease_lost.set()
                raise LeaseLost(f"Lease lost for {adapter.name} job {job_id}")
            if int(current.get("cancel_requested") or 0):
                cancel_requested.set()
                raise WorkCancelled(f"Cancelled {adapter.name} job {job_id}")
            if int(current.get("pause_requested") or 0):
                pause_requested.set()
                raise WorkPaused(f"Paused {adapter.name} job {job_id}")

        try:
            adapter.dispatch(job, progress, should_cancel)
            if lease_lost.is_set():
                raise LeaseLost(f"Lease lost for {adapter.name} job {job_id}")
            if cancel_requested.is_set():
                raise WorkCancelled(f"Cancelled {adapter.name} job {job_id}")
            if pause_requested.is_set():
                raise WorkPaused(f"Paused {adapter.name} job {job_id}")
            completed, _ = self._call_adapter(
                adapter,
                "Completing job",
                lambda: adapter.complete(job_id, lease_token),
            )
            if not completed:
                self._report_error(
                    f"Completing {adapter.name} job {job_id}",
                    RuntimeError("Adapter completion failed after retries; lease will recover"),
                )
        except LeaseLost as exc:
            self._report_error(f"Lease lost for {adapter.name} job {job_id}", exc)
        except (WorkCancelled, WorkPaused):
            # ``complete`` turns the persisted request into cancelled or paused.
            completed, _ = self._call_adapter(
                adapter,
                "Completing cancelled job",
                lambda: adapter.complete(job_id, lease_token),
            )
            if not completed:
                self._report_error(
                    f"Completing cancelled {adapter.name} job {job_id}",
                    RuntimeError("Adapter completion failed after retries; lease will recover"),
                )
        except PermanentWorkError as exc:
            failed, _ = self._call_adapter(
                adapter,
                "Recording permanent job failure",
                lambda: adapter.fail(job_id, lease_token, str(exc), False),
            )
            if not failed:
                self._report_error(
                    f"Recording permanent failure for {adapter.name} job {job_id}",
                    RuntimeError("Adapter failure update failed after retries; lease will recover"),
                )
            self._report_error(f"Permanent failure in {adapter.name} job {job_id}", exc)
        except BaseException as exc:
            failed, _ = self._call_adapter(
                adapter,
                "Recording retryable job failure",
                lambda: adapter.fail(job_id, lease_token, str(exc), True),
            )
            if not failed:
                self._report_error(
                    f"Recording retryable failure for {adapter.name} job {job_id}",
                    RuntimeError("Adapter failure update failed after retries; lease will recover"),
                )
            self._report_error(f"Failure in {adapter.name} job {job_id}", exc)
        finally:
            heartbeat_done.set()
            heartbeat_thread.join(timeout=min(2.0, self.heartbeat_seconds))
            self._refresh_process_lease()
